"""Server composition for Cloud.ru/S3 screenshots; never creates a bucket."""
from pathlib import Path
import os
from urllib.parse import urlsplit

from .catalogue_images import CatalogueImageStore


class ScreenshotStorageConfigurationError(RuntimeError):
    pass


def _secret(value):
    return value.get_secret_value() if hasattr(value, 'get_secret_value') else str(value or '')


def _sdk_client(**options):
    try:
        import boto3
        from botocore.config import Config
        from botocore.session import Session
        from botocore.loaders import Loader
        import botocore
    except ImportError:
        raise ScreenshotStorageConfigurationError('s3_sdk_unavailable') from None
    access = options.pop('aws_access_key_id')
    secret = options.pop('aws_secret_access_key')
    region = options.pop('region_name')
    core = Session()
    # Boto still loads profile configuration even with explicit credentials.
    # Isolate both profile files and the ambient AWS_PROFILE selection.
    core.set_config_variable('config_file', os.devnull)
    core.set_config_variable('credentials_file', os.devnull)
    core.get_component('config_store').set_config_variable('profile', None)
    core.set_config_variable('region', region)
    core.set_credentials(access, secret)
    core.register_component('data_loader', Loader(
        extra_search_paths=[str(Path(botocore.__file__).parent/'data')],
        include_default_search_paths=False))
    session = boto3.session.Session(botocore_session=core)
    return session.client('s3', config=Config(**options.pop('config')), **options)


def create_image_store(root: Path, *, settings=None, client_factory=None):
    """No transport when unconfigured; incomplete configuration fails explicitly.

    Credentials are supplied directly to the SDK, never read from AWS profiles or
    obtained from VM metadata. The provider API key is unrelated to S3 access.
    """
    if settings is None:
        from ..config import settings
    bucket = str(getattr(settings, 's3_bucket', '') or '')
    access = _secret(getattr(settings, 's3_access_key_id', ''))
    secret = _secret(getattr(settings, 's3_secret_access_key', ''))
    prefix = str(getattr(settings, 's3_screenshot_prefix', 'catalogue/screenshots'))
    if not any((bucket, access, secret)):
        return CatalogueImageStore(root, prefix=prefix)
    endpoint = str(getattr(settings, 's3_endpoint_url', '') or '')
    parsed = urlsplit(endpoint)
    if (not all((bucket, access, secret, endpoint)) or parsed.scheme != 'https'
            or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ScreenshotStorageConfigurationError('s3_configuration_incomplete_or_invalid')
    factory = client_factory or _sdk_client
    client = factory(endpoint_url=endpoint,
        region_name=str(getattr(settings, 's3_region', 'ru-central-1')),
        aws_access_key_id=access, aws_secret_access_key=secret,
        config=dict(signature_version='s3v4', connect_timeout=5, read_timeout=15,
                    retries={'mode': 'standard', 'total_max_attempts': 3},
                    proxies={}, s3={'addressing_style': 'path'}))
    return CatalogueImageStore(root, client=client, bucket=bucket, prefix=prefix)
