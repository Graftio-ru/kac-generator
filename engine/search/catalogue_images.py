"""Retain producer screenshots and upload immutable artifacts, without OCR.

The native card producer supplies bytes and the observation binding. This module
does not certify the image contents or fetch supplier pages. An injected S3
client owns authentication; no bucket or credentials are created here.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import re
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from concurrent.futures import CancelledError

from .failure_policy import ResearchControlError


class S3Client(Protocol):
    def put_object(self, **kwargs): ...
    def head_object(self, **kwargs): ...
    def get_object(self, **kwargs): ...


class CatalogueImageStore:
    def __init__(self, root: Path, *, client: S3Client | None = None,
                 bucket: str = "", prefix: str = "catalogue/screenshots",
                 max_bytes: int = 20 * 1024 * 1024):
        self.root = Path(root)
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if not self.prefix or any(p in {"", ".", ".."} for p in self.prefix.split("/")):
            raise ValueError("Invalid artifact prefix")
        if max_bytes <= 0:
            raise ValueError("Invalid artifact size limit")
        self.max_bytes = max_bytes

    def materialize(self, image: dict, *, observation_id: str, url: str,
                    observed_at: float) -> dict:
        """Read an immutable object into its recorded owned filename; never PUT.

        Caller resolves the observation from server storage. SDK transport keeps
        its configured timeouts; byte/deadline/control checks run between reads.
        An unavailable image cannot change any price or source observation.
        """
        result = dict(status='error', local_path='', error='catalogue_image_binding_invalid')
        body = None
        try:
            if type(image) is not dict or image.get('status') != 'captured':return result
            if (image.get('observation_id') != observation_id or image.get('source_url') != url
                    or image.get('observed_at') != observed_at):return result
            parsed = urlsplit(url)
            if (type(observation_id) is not str or not observation_id or len(observation_id)>256
                    or parsed.scheme not in {'http','https'} or not parsed.hostname
                    or parsed.username or parsed.password):return result
            captured_at=image.get('captured_at')
            for stamp in (observed_at,captured_at):
                if type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<=0:return result
            if captured_at<observed_at:return result
            digest=image.get('sha256','');size=image.get('size')
            if type(digest) is not str or not re.fullmatch('[a-f0-9]{64}',digest) or type(size) is not int or not 0<size<=self.max_bytes:return result
            binding=hashlib.sha256(observation_id.encode()).hexdigest()
            filename=f'{binding}-{digest}.png'
            if image.get('local_path') != filename:return result
            if self.root.is_symlink():return result
            root=self.root.resolve();destination=root/filename
            if destination.is_symlink():return result
            if destination.exists():
                if destination.stat().st_size!=size or hashlib.sha256(destination.read_bytes()).hexdigest()!=digest:
                    return dict(result,error='catalogue_image_local_hash_mismatch')
                return dict(status='local',local_path=str(destination),error='')
            key=f'{self.prefix}/{binding}/{digest}.png'
            if self.client is None or not self.bucket:return dict(result,error='s3_not_configured')
            if image.get('bucket')!=self.bucket or image.get('key')!=key:
                return dict(result,error='catalogue_image_s3_binding_mismatch')
            from .session import bounded_timeout
            bounded_timeout(1)
            response=self.client.get_object(Bucket=self.bucket,Key=key)
            body=response.get('Body')
            context=json.dumps(dict(observation_id=observation_id,url=url,observed_at=observed_at,
                captured_at=captured_at,sha256=digest),sort_keys=True).encode()
            expected={'sha256':digest,'observation':binding,'context':hashlib.sha256(context).hexdigest()}
            code=response.get('ResponseMetadata',{}).get('HTTPStatusCode')
            if (type(code) is not int or not 200<=code<300 or response.get('Metadata')!=expected
                    or type(response.get('ContentLength')) is not int or response['ContentLength']!=size
                    or response.get('ContentType')!='image/png' or response.get('ContentEncoding') not in (None,'identity','','aws-chunked')
                    or not callable(getattr(body,'read',None))):
                return dict(result,error='catalogue_image_s3_binding_mismatch')
            deadline=time.monotonic()+30;data=bytearray()
            while True:
                bounded_timeout(1)
                if time.monotonic()>=deadline:return dict(result,error='s3_image_read_budget')
                chunk=body.read(min(8192,size-len(data)+1))
                bounded_timeout(1)
                if time.monotonic()>=deadline:return dict(result,error='s3_image_read_budget')
                if not chunk:break
                if type(chunk) is not bytes:return dict(result,error='s3_image_body_invalid')
                data.extend(chunk)
                if len(data)>size:return dict(result,error='s3_image_size_mismatch')
            if len(data)!=size or hashlib.sha256(data).hexdigest()!=digest:return dict(result,error='s3_image_hash_mismatch')
            bounded_timeout(1)
            root.mkdir(parents=True,exist_ok=True)
            descriptor,temporary=tempfile.mkstemp(prefix='.download-',dir=root)
            try:
                with os.fdopen(descriptor,'wb') as output:output.write(data);output.flush();os.fsync(output.fileno())
                bounded_timeout(1)
                if time.monotonic()>=deadline:return dict(result,error='s3_image_read_budget')
                try:os.link(temporary,destination)
                except FileExistsError:
                    if destination.is_symlink() or destination.read_bytes()!=data:return dict(result,error='catalogue_image_local_hash_mismatch')
            finally:Path(temporary).unlink(missing_ok=True)
            return dict(status='materialized',local_path=str(destination),error='')
        except (CancelledError,ResearchControlError):raise
        except Exception as error:
            response=getattr(error,'response',{})
            code=response.get('Error',{}).get('Code') if type(response) is dict else None
            return dict(result,error='s3_image_not_found' if code in {'NoSuchKey','NotFound','404'} else 's3_image_get_failed')
        finally:
            if callable(getattr(body,'close',None)):
                try:body.close()
                except Exception:pass

    def retain(self, data: bytes, *, observation_id: str, url: str,
               observed_at: float, captured_at: float) -> dict:
        """Retain one actual screenshot; retries cannot mutate old S3 objects.

        Date assertions belong to the producer. Upload failure retains the local
        file and reports a safe code instead of leaking SDK/credential errors.
        """
        if not observation_id or not isinstance(observation_id, str):
            raise ValueError("Observation id required")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("Public card URL required")
        for stamp in (observed_at, captured_at):
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
                raise ValueError("Actual observation/capture dates required")
        if not isinstance(data, bytes) or not data or len(data) > self.max_bytes:
            raise ValueError("Screenshot missing or too large")
        digest = hashlib.sha256(data).hexdigest()
        binding = hashlib.sha256(observation_id.encode("utf-8")).hexdigest()
        filename = f"{binding}-{digest}.png"
        if self.root.is_symlink():
            raise ValueError("Artifact root cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        context = json.dumps(dict(observation_id=observation_id, url=url,
                                  observed_at=observed_at, captured_at=captured_at,
                                  sha256=digest), sort_keys=True).encode()
        context_path = self.root / f"{binding}.binding.json"
        if context_path.is_symlink():
            raise ValueError("Artifact binding cannot be a symlink")
        descriptor, temporary = tempfile.mkstemp(prefix=".binding-", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(context)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, context_path)
            except FileExistsError:
                if context_path.is_symlink() or context_path.read_bytes() != context:
                    raise ValueError("Observation screenshot binding is immutable")
        finally:
            Path(temporary).unlink(missing_ok=True)
        destination = self.root / filename
        # Never follow an existing symlink or overwrite a conflicting artifact.
        if destination.is_symlink():
            raise ValueError("Artifact path cannot be a symlink")
        if destination.exists():
            if destination.read_bytes() != data:
                raise ValueError("Artifact collision")
        else:
            descriptor, temporary = tempfile.mkstemp(prefix=".capture-", dir=self.root)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                # Hard link publishes without replacing an existing immutable file.
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    if destination.is_symlink() or destination.read_bytes() != data:
                        raise ValueError("Artifact collision")
            finally:
                Path(temporary).unlink(missing_ok=True)
        key = f"{self.prefix}/{binding}/{digest}.png"
        result = dict(observation_id=observation_id, url=url,
                      observed_at=observed_at, captured_at=captured_at,
                      local_path=str(destination.resolve()), sha256=digest,
                      size=len(data), bucket="", key="", status="local",
                      error="")
        if self.client is None or not self.bucket:
            result["error"] = "s3_not_configured"
            return result
        metadata = {"sha256": digest, "observation": binding,
                    "context": hashlib.sha256(context).hexdigest()}
        try:
            response = self.client.put_object(
                Bucket=self.bucket, Key=key, Body=data, ContentType="image/png",
                Metadata=metadata,
                IfNoneMatch="*")
            code = (response or {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if not isinstance(code, int) or not 200 <= code < 300:
                raise RuntimeError("Object upload was not acknowledged")
        except (CancelledError, ResearchControlError):
            raise
        except Exception as error:
            # Conditional retry is successful only if the existing object's
            # original snapshot binding AND actual length are acknowledged.
            failure = getattr(error, "response", {})
            code = failure.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(failure, dict) else None
            if code == 412:
                try:
                    existing = self.client.head_object(Bucket=self.bucket, Key=key)
                    acknowledged = existing.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    if (type(acknowledged) is int and 200 <= acknowledged < 300
                            and existing.get("Metadata") == metadata
                            and existing.get("ContentLength") == len(data)):
                        result.update(bucket=self.bucket, key=key, status="stored")
                        return result
                except (CancelledError, ResearchControlError):
                    raise
                except Exception:
                    pass
            result.update(status="upload_failed", error="s3_upload_failed")
            return result
        result.update(bucket=self.bucket, key=key, status="stored")
        return result
