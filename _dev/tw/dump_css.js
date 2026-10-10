const fs=require('fs'), path=require('path'), {JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..','..');
const tw=fs.readFileSync(path.join(root,'static/vendor/tailwind.js'),'utf8');
const page=process.argv[2];
process.on('unhandledRejection', r => console.log('UNHANDLED:', String((r&&r.reason)||r).split('\n')[0].slice(0,300)));
const html=fs.readFileSync(page,'utf8').replace(/<script src="\/static\/vendor\/tailwind\.js"><\/script>/,'');
const dom=new JSDOM(html,{runScripts:'outside-only',pretendToBeVisual:true,url:'http://x/'});
const w=dom.window;
w.console.warn=(...a)=>{const s=String(a[0]); if(!/production/.test(s)) console.log('WARN:',s.slice(0,300));};
w.console.error=(...a)=>console.log('ERR:',String(a[0]).split('\n')[0].slice(0,300));
w.eval(tw);
w.document.querySelectorAll('script:not([src])').forEach(t=>{try{w.eval(t.textContent);}catch(_){}});
setTimeout(()=>{
  const css=[...w.document.querySelectorAll('style')].filter(s=>!s.getAttribute('type')).map(s=>s.textContent).join('\n');
  fs.writeFileSync(process.argv[3], css);
  console.log('wrote', css.length, 'bytes to', process.argv[3]);
  console.log('tail 400 chars:\n' + css.slice(-400));
},Number(process.argv[4]||4000));
