const fs=require('fs'), path=require('path'), {JSDOM}=require('jsdom');
const tw=fs.readFileSync(path.resolve(__dirname,'..','..','static/vendor/tailwind.js'),'utf8');
const html=fs.readFileSync(process.argv[2],'utf8');
const dom=new JSDOM(html,{runScripts:'outside-only',pretendToBeVisual:true,url:'http://x/'});
const w=dom.window; const seen=[];
const note=t=>{const s=String(t).split('\n')[0].slice(0,220); if(!/production/.test(s)) seen.push(s);};
process.on('unhandledRejection',r=>note((r&&r.reason)||r));
w.console.warn=(...a)=>note(a[0]); w.console.error=(...a)=>note(a[0]);
w.eval(tw);
w.document.querySelectorAll('script:not([src])').forEach(t=>{ if(/tailwind\.config/.test(t.textContent)){try{w.eval(t.textContent);}catch(_){}}});
setTimeout(()=>{
  const css=[...w.document.querySelectorAll('style')].filter(s=>!s.getAttribute('type')).map(s=>s.textContent).join('');
  console.log(path.basename(process.argv[2]).padEnd(26), (/\.flex\{/.test(css)?'OK   ':'BREAK'), String(css.length).padStart(6)+'b');
  seen.slice(0,3).forEach(s=>console.log('    '+s));
},5000);
