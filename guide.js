(async()=>{
  const finish=confirmed=>window.chrome.webview.postMessage({confirmed});
  try{
    const response=await fetch('index.html');if(!response.ok)throw new Error('指南文件读取失败');
    const source=new DOMParser().parseFromString(await response.text(),'text/html');
    const body=source.querySelector('.guide-body');const footer=body.querySelector('.guide-footer-actions');
    const connecting=new URLSearchParams(location.search).get('connecting')==='1';
    footer.querySelector('#guide-cancel-btn').hidden=!connecting;
    footer.querySelector('#guide-done-btn').textContent=connecting?'我已知晓，继续连接':'我知道了';
    document.body.replaceChildren(body,footer);
    document.querySelector('#guide-cancel-btn').onclick=()=>finish(false);
    document.querySelector('#guide-done-btn').onclick=()=>finish(true);
    document.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();finish(false);}});
  }catch{
    document.body.textContent='指南加载失败，请关闭窗口后重试。';
    const close=document.createElement('button');close.textContent='关闭';close.onclick=()=>finish(false);document.body.append(close);
  }
})();
