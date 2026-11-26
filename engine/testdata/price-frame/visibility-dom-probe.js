// OFFLINE DOM contract; executes the production visibility function unchanged.
const fs=require('fs'), assert=require('assert');
const check=eval('('+fs.readFileSync(0,'utf8')+')');
global.innerWidth=1366;global.innerHeight=900;global.scrollX=0;global.scrollY=0;
global.location={href:'https://supplier.example/product/ABC123'};global.NodeFilter={SHOW_TEXT:4};
function element(text,box,parent=null,style={}) {
 const e={innerText:text,parentElement:parent,style:{display:'block',visibility:'visible',opacity:'1',position:'static',pointerEvents:'auto',transform:'none',filter:'none',zIndex:'auto',...style},
  getBoundingClientRect(){return {...box,left:box.x,top:box.y,right:box.x+box.width,bottom:box.y+box.height}},
  contains(other){for(let p=other;p;p=p.parentElement)if(p===this)return true;return false}};return e;
}
global.getComputedStyle=(e,pseudo)=>pseudo?(e.pseudo||{content:'none',display:'block',visibility:'visible',opacity:'1',backgroundColor:'rgba(0,0,0,0)',backgroundImage:'none',boxShadow:'none'}):e.style;
const heading=element('Item ABC123',{x:20,y:200,width:500,height:30});
const span=element('Item ABC123',{x:20,y:200,width:200,height:30},heading);
const text={textContent:'Item ABC123',parentElement:span};
let nodes=[heading,span],top=span;
global.document={querySelectorAll:()=>nodes,createTreeWalker:()=>{let next=true;return{nextNode(){if(!next)return null;next=false;return text}}},
 createRange:()=>({setStart(node,offset){assert.strictEqual(node,text);assert.equal(offset,0)},setEnd(node,offset){assert.strictEqual(node,text);assert.equal(offset,text.textContent.length)},getClientRects:()=>[span.getBoundingClientRect()]}),elementFromPoint:()=>top};
const required=[{role:'heading',text:heading.innerText,...heading.getBoundingClientRect()}];
assert.equal(check(required).verified,true,'legitimate inline glyph owner');
for(const [name,style,parent,box] of [
 ['sticky',{position:'sticky'},null,{x:0,y:200,width:1000,height:15}],
 ['pointer-none',{position:'absolute',pointerEvents:'none'},null,{x:20,y:200,width:5,height:30}],
 ['covering-child',{position:'absolute'},heading,{x:20,y:200,width:200,height:30}],
 ['static-transform-pointer-none',{position:'static',pointerEvents:'none',transform:'matrix(1,0,0,1,40,220)'},null,{x:20,y:200,width:1000,height:60}],
 ['partial-fixed',{position:'fixed'},null,{x:21,y:202,width:1,height:1}],
]) {
 const overlay=element('',box,parent,style);nodes=[heading,span,overlay];top=name==='covering-child'?overlay:span;
 assert.equal(check(required).verified,false,name+' must refuse');
}
nodes=[heading,span];top=span;global.innerHeight=210;
assert.equal(check(required).verified,false,'offscreen lower glyph samples');
global.innerHeight=900;heading.pseudo={content:'\"\"',position:'absolute',display:'block',visibility:'visible',opacity:'1',backgroundColor:'rgb(0,0,0)',backgroundImage:'none',boxShadow:'none',pointerEvents:'none'};
assert.equal(check(required).verified,false,'opaque required-container pseudo paint is unverifiable');
heading.pseudo=null;const distant=element('',{x:0,y:1700,width:1,height:1});distant.pseudo={content:'\"\"',position:'fixed',display:'block',visibility:'visible',opacity:'1',backgroundColor:'rgb(0,0,0)',backgroundImage:'none',boxShadow:'none',pointerEvents:'none'};nodes=[heading,span,distant];
assert.equal(check(required).verified,false,'pseudo extent cannot borrow distant owner geometry');
console.log('9 production visibility DOM assertions passed');
