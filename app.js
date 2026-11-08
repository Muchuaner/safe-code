const $ = selector => document.querySelector(selector);
const appWindow = $('#window');
let connectionTimer;
let ipQueryController;
let serverConnection = { ingressIp: '', egressIp: '' };
let ipPolicy = { lockIngress: false, lockEgress: false };
let detectedRegion = 'us_ny';
let localeSelection = 'auto';
const localePresets = {
  jp: {name:'日本',timezone:'Asia/Tokyo',language:'日语',locale:'ja_JP.UTF-8'},
  us_ny: {name:'美国 · 纽约',timezone:'America/New_York',language:'英语（美国）',locale:'en_US.UTF-8'},
  us_la: {name:'美国 · 洛杉矶',timezone:'America/Los_Angeles',language:'英语（美国）',locale:'en_US.UTF-8'},
  sg: {name:'新加坡',timezone:'Asia/Singapore',language:'英语（新加坡）',locale:'en_SG.UTF-8'},
  gb: {name:'英国',timezone:'Europe/London',language:'英语（英国）',locale:'en_GB.UTF-8'},
  de: {name:'德国',timezone:'Europe/Berlin',language:'德语',locale:'de_DE.UTF-8'},
  kr: {name:'韩国',timezone:'Asia/Seoul',language:'韩语',locale:'ko_KR.UTF-8'}
};
function currentLocale(){return localePresets[localeSelection==='auto'?detectedRegion:localeSelection]||null;}
let runToken = 0;
let busy = false;
let currentMode = 'deployment';
let mountPairs = [];
const advancedTimers = new Set();
let selecting = false;
let selectedTasks = new Set();
let states = [];
let taskLogs = [];
const tasks = [
  ['设置工作用户', '创建日常开发账户，配置用户权限与 SSH 密钥。'],
  ['本土化设置', '按出口 IP 所在地匹配时区与系统语言。'],
  ['安装虚拟桌面', '配置 XFCE 与远程桌面，保留断线后的工作状态。'],
  ['安装 Chrome', '安装 Chrome 浏览器，配置桌面启动入口。'],
  ['安装中文输入法', '配置 Fcitx5 拼音输入法与中文字体。'],
  ['安装 Claude Code', '准备 Claude Code、Git 和 tmux 开发工具。'],
  ['配置 SSH 安全通道', '通过加密隧道连接终端与远程桌面。']
];
states = Array(tasks.length).fill('pending');
taskLogs = Array(tasks.length).fill('');
function showScreen(name) {
  for(const id of ['notice','login','workspace']) $('#'+id+'-screen').hidden = id!==name;
  appWindow.classList.toggle('wide',name==='workspace');
}
function stopWork(){for(const timer of advancedTimers)clearTimeout(timer);advancedTimers.clear();if(ipQueryController)ipQueryController.abort();clearTimeout(connectionTimer);runToken++;busy=false;}
function exit(){stopWork();$('#login-form').reset();appWindow.hidden=true;$('#restore-window').hidden=true;$('#ip-policy-dialog').close();$('#localization-dialog').close();$('#mount-dialog').close();$('#maintenance-result').close();$('#result-body').replaceChildren();$('#client-window').close();$('#client-frame').removeAttribute('src');document.title='Safe Code · 已退出';}
$('#close').onclick=exit;
$('#exit').onclick=exit;
$('#minimize').onclick=()=>{appWindow.hidden=true;$('#restore-window').hidden=false;};
$('#restore-window').onclick=()=>{appWindow.hidden=false;$('#restore-window').hidden=true;};
$('#maximize').onclick=()=>{
  const expanded=appWindow.classList.toggle('maximized');
  $('#maximize').setAttribute('aria-label',expanded?'还原窗口':'最大化');
  $('#maximize').title=expanded?'还原窗口':'最大化';
  $('#maximize').classList.toggle('is-maximized',expanded);
};
function switchMode(mode){
  currentMode=mode;
  $('#deployment-panel').hidden=mode!=='deployment';$('#maintenance-panel').hidden=mode!=='maintenance';
  for(const value of ['deployment','maintenance']){
    const button=$('#'+value+'-mode');button.classList.toggle('active',mode===value);button.setAttribute('aria-pressed',String(mode===value));
  }
}
$('#deployment-mode').onclick=()=>switchMode('deployment');
$('#maintenance-mode').onclick=()=>switchMode('maintenance');
$('#close-result').onclick=()=>$('#maintenance-result').close();
$('#account-done').onclick=()=>$('#maintenance-result').close();
$('#account-card').onclick=()=>{
  $('#result-body').replaceChildren();
  for(const [label,value] of [['用户名','workspace'],['密码','SafeCode-demo-2026']]){
    const row=document.createElement('div');row.className='credential-row';const name=document.createElement('span');name.textContent=label;const content=document.createElement('code');content.textContent=value;const valueGroup=document.createElement('div');valueGroup.className='credential-value';
    const copy=document.createElement('button');copy.type='button';copy.className='copy-button';copy.setAttribute('aria-label','复制'+label);copy.title='复制'+label;
    const icon='<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="7" y="7" width="9" height="10" rx="1.5"/><path d="M12 7V3H3v10h4"/></svg>';
    copy.innerHTML=icon;
    copy.onclick=async()=>{
      try{
        await navigator.clipboard.writeText(value);
        copy.innerHTML='<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m4 10 4 4 8-9"/></svg>';copy.title='已复制';copy.setAttribute('aria-label',label+'已复制');
        setTimeout(()=>{copy.innerHTML=icon;copy.title='复制'+label;copy.setAttribute('aria-label','复制'+label);},1600);
      }catch{copy.title='复制失败，请手动选择复制';copy.setAttribute('aria-label','复制失败，请手动选择'+label);}
    };
    valueGroup.append(content,copy);row.append(name,valueGroup);$('#result-body').append(row);
  }
  $('#maintenance-result').showModal();
};
function launchClient(client){
  $('#client-window-title').textContent=client==='cloud'?'CloudCLI':client==='terminal'?'终端':'远程桌面连接';
  $('#client-frame').src='client-preview.html?client='+client;
  $('#client-window').showModal();
}
$('#close-client').onclick=()=>{$('#client-window').close();$('#client-frame').removeAttribute('src');};
$('#terminal-card').onclick=()=>launchClient('terminal');
$('#desktop-card').onclick=()=>launchClient('desktop');
$('#accept').onclick=()=>{showScreen('login');$('#ip').focus();};
$('#back').onclick=()=>{stopWork();$('#login-form').reset();resetLogin();showScreen('notice');};
$('#reveal').onclick=()=>{const visible=$('#password').type==='password';$('#password').type=visible?'text':'password';$('#reveal').textContent=visible?'隐藏':'显示';$('#reveal').setAttribute('aria-label',visible?'隐藏密码':'显示密码');};
function resetLogin(){
  $('#login-form').querySelectorAll('input,button').forEach(el=>el.disabled=false);
  $('#back').disabled=false;$('#password').type='password';$('#reveal').textContent='显示';$('#reveal').setAttribute('aria-label','显示密码');
  $('#connect').innerHTML='连接服务器 <span>→</span>';
  $('#connection-message').className='connection-message';$('#connection-message').textContent='';
}
$('#login-form').onsubmit=event=>{
  event.preventDefault();const ip=$('#ip').value.trim();const username=$('#username').value.trim();
  if(!ip || !username){$('#connection-message').textContent='请填写 IP 地址和用户名。';$('#connection-message').className='connection-message error';return;}
  $('#login-form').querySelectorAll('input,button').forEach(el=>el.disabled=true);$('#back').disabled=true;
  $('#connect').textContent='正在连接…';$('#connection-message').textContent='正在建立 SSH 连接，请稍候…';$('#connection-message').className='connection-message connecting';
  connectionTimer=setTimeout(()=>{
    serverConnection = { ingressIp: ip, egressIp: ip };
    $('#password').value='';$('#server-ingress').textContent=serverConnection.ingressIp;$('#server-address').textContent=serverConnection.egressIp;$('#server-user').textContent=username;
    states=Array(tasks.length).fill('pending');taskLogs=Array(tasks.length).fill('');selecting=false;selectedTasks.clear();renderTasks();resetLogin();switchMode('deployment');$('#maintenance-result').close();showScreen('workspace');queryIpInfo(serverConnection.egressIp);
  },1400);
};
function renderTasks(){
  $('#steps').replaceChildren(...tasks.map(([title,description],i)=>{
    const row=document.createElement('article');row.className='step '+states[i];
    const number=document.createElement('span');number.className='step-number';number.textContent=String(i+1);
    const copy=document.createElement('div');copy.className='step-copy';
    const heading=document.createElement('h3');heading.textContent=title;
    const desc=document.createElement('p');desc.className=taskLogs[i]?'task-log':'';const preset=currentLocale();const detail=i===1?(preset?preset.name+' · '+preset.timezone+' · '+preset.locale:'未能自动匹配所在地，请先设置时区与语言。'):description;desc.textContent=taskLogs[i]||detail;desc.setAttribute('role','status');desc.setAttribute('aria-live','polite');copy.append(heading,desc);
    const button=document.createElement('button');button.className='deploy-button';button.textContent=states[i]==='done'?'已完成 ✓':states[i]==='running'?'部署中…':'开始部署';button.disabled=busy||states[i]==='done'||(i===1&&!currentLocale());button.setAttribute('aria-label',title+'：'+button.textContent);button.onclick=()=>deployOne(i);
    if(selecting){
      const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.className='task-checkbox';checkbox.disabled=states[i]==='done'||busy||(i===1&&!currentLocale());checkbox.checked=states[i]!=='done'&&selectedTasks.has(i);checkbox.setAttribute('aria-label','选择'+title);
      checkbox.onchange=()=>{if(checkbox.checked)selectedTasks.add(i);else selectedTasks.delete(i);updateBatchControls();};
      row.append(checkbox);if(states[i]==='done')row.classList.add('unselectable');
    }
    if(i===1){const settings=document.createElement('button');settings.className='text-button locale-settings-button';settings.textContent='设置';settings.setAttribute('aria-label','设置本土化');settings.disabled=busy;settings.onclick=openLocalization;row.append(number,copy,settings,button);}else row.append(number,copy,button);return row;
  }));
  const completed=states.filter(state=>state==='done').length;
  $('#overall-status').textContent=completed===tasks.length?'全部配置完成':completed+' / '+tasks.length+' 已完成';
  $('#overall-detail').textContent=completed===tasks.length?'开发环境已就绪。':'可单项部署，或选择所需项目批量部署。';
  $('#deploy-all').disabled=busy||completed===tasks.length;$('#deploy-all').textContent=completed===tasks.length?'部署完成 ✓':busy?'正在部署…':'一键部署';
  updateBatchControls();
}
function updateBatchControls(){
  $('#cancel-selection').hidden=!selecting;
  $('.deployment-heading > span').textContent=selecting?'勾选要部署的项目，已完成项不可选。':'按步骤部署，或一次完成。';
  if(selecting){
    $('#deploy-all').textContent='开始部署（'+selectedTasks.size+' 项）';
    $('#deploy-all').disabled=busy||selectedTasks.size===0;
    $('#overall-detail').textContent='已选择 '+selectedTasks.size+' 项，仅执行勾选的步骤。';
  }
}
const deploymentLogs = [
  ['检查工作账户是否存在…', '创建开发账户与主目录…', '配置用户权限…', '生成并校验 SSH 密钥…', '工作账户配置完成。'],
  [],
  ['检查桌面组件与磁盘空间…', '下载 XFCE 桌面组件… 32%', '安装 XFCE 与 xrdp… 76%', '配置桌面会话与断线保留…', '虚拟桌面安装完成。'],
  ['检查浏览器安装环境…', '下载 Chrome 安装包… 38%', '安装 Chrome 浏览器…', '配置桌面启动入口…', 'Chrome 浏览器已就绪。'],
  ['检查中文输入支持…', '安装中文字体… 38%', '安装 Fcitx5 与拼音组件…', '配置中文输入与快捷键…', '中文输入法配置完成。'],
  ['检查 Git 与 tmux 依赖…', '下载 Claude Code… 45%', '安装 Claude Code 原生程序…', '校验程序版本与执行权限…', '开发工具安装完成，等待账号授权。'],
  ['检查 SSH 与桌面监听端口…', '建立本地加密隧道…', '验证 SSH 通道连通性…', '检查远程桌面转发…', 'SSH 安全通道配置完成。']
];
function updateTaskLog(index,message){
  const time=new Date().toLocaleTimeString('zh-CN',{hour12:false});
  taskLogs[index]='['+time+'] '+message;
  renderTasks();
}
async function simulateTask(index,token){
  if(index===1&&!currentLocale())return false;
  states[index]='running';
  const preset=currentLocale();
  const messages=index===1?(preset?['核对出口 IP 归属地…','设置时区为 '+preset.timezone+'…','生成 '+preset.locale+' 语言环境…','应用'+preset.language+'系统语言与时间格式…','本土化设置完成：'+preset.timezone+' / '+preset.locale]:['无法确定所在地，请先完成本土化设置。']):deploymentLogs[index];
  for(let i=0;i<messages.length;i++){
    if(token!==runToken)return false;
    if(i===messages.length-1)states[index]='done';
    updateTaskLog(index,messages[i]);
    if(i<messages.length-1)await new Promise(resolve=>setTimeout(resolve,1100));
  }
  return true;
}
async function deployOne(index){if(busy||states[index]==='done')return;selecting=false;selectedTasks.clear();busy=true;const token=++runToken;await simulateTask(index,token);if(token!==runToken)return;busy=false;renderTasks();}
$('#deploy-all').onclick=async()=>{
  if(busy)return;
  if(!selecting){selecting=true;selectedTasks.clear();renderTasks();return;}
  const queue=[...selectedTasks].filter(i=>states[i]!=='done').sort((a,b)=>a-b);
  if(!queue.length)return;
  selecting=false;selectedTasks.clear();busy=true;const token=++runToken;
  for(const i of queue){if(!await simulateTask(i,token)){if(token===runToken){busy=false;renderTasks();}return;}}
  if(token!==runToken)return;busy=false;renderTasks();
};
$('#cancel-selection').onclick=()=>{selecting=false;selectedTasks.clear();renderTasks();};
renderTasks();

function isPublicIpv4(ip){
  const octets=ip.split('.');if(octets.length!==4||octets.some(n=>!/^\d{1,3}$/.test(n)||Number(n)>255))return false;
  const [a,b,c]=octets.map(Number);
  return !(a===0||a===10||a===127||a>=224||(a===169&&b===254)||(a===172&&b>=16&&b<=31)||(a===192&&b===168)||(a===100&&b>=64&&b<=127)||(a===192&&b===0&&(c===0||c===2))||(a===198&&(b===18||b===19||(b===51&&c===100)))||(a===203&&b===0&&c===113));
}
async function queryIpInfo(ip){
  if(ipQueryController)ipQueryController.abort();
  if(!isPublicIpv4(ip)){
    detectedRegion='us_ny';renderTasks();
    $('#rating-state').textContent='';$('#ip-location').textContent='美国 · 纽约';$('#ip-score').textContent='85';$('#score-unit').textContent=' / 100';
    $('#score-source').textContent='Net.Coffee · IP 评分';$('#score-source').href='https://ip.net.coffee/claude/';
    $('#score-source').title='当前为示例 IP，归属地与评分仅为界面示意；真实查询暂支持公网 IPv4。';return;
  }
  detectedRegion=null;renderTasks();
  const controller=new AbortController();ipQueryController=controller;
  const timeout=setTimeout(()=>controller.abort(),12000);
  $('#rating-state').textContent='查询中';$('#ip-location').textContent='正在查询…';$('#ip-score').textContent='—';$('#score-unit').textContent='';
  $('#score-source').textContent='Net.Coffee · IP 评分';$('#score-source').href='https://ip.net.coffee/ip/'+encodeURIComponent(ip);
  try{
    const results=await Promise.allSettled(['geoip','iprisk'].map(async endpoint=>{
      const response=await fetch('https://ip.net.coffee/api/'+endpoint+'/'+encodeURIComponent(ip),{signal:controller.signal,credentials:'omit',referrerPolicy:'no-referrer'});
      if(!response.ok)throw new Error('查询失败');return response.json();
    }));
    if(ipQueryController!==controller)return;
    const geo=results[0].status==='fulfilled'?results[0].value:null;
    const risk=results[1].status==='fulfilled'?results[1].value:null;
    const countryCode=(geo?.country_code||risk?.countryCode||'').toLowerCase();
    const regions={jp:'jp',sg:'sg',gb:'gb',de:'de',kr:'kr'};
    detectedRegion=countryCode==='us'?({'New York':'us_ny','Los Angeles':'us_la'}[geo?.city]||null):(regions[countryCode]||null);
    renderTasks();
    const country={ 'United States':'美国','Singapore':'新加坡','Japan':'日本','Germany':'德国','United Kingdom':'英国','Canada':'加拿大','Australia':'澳大利亚','China':'中国','Netherlands':'荷兰' };
    const city={ 'New York':'纽约','Los Angeles':'洛杉矶','Singapore':'新加坡','Tokyo':'东京' };
    $('#ip-location').textContent=geo&&geo.country?[country[geo.country]||geo.country,city[geo.city]||geo.city].filter(Boolean).join(' · '):'归属地暂无数据';
    const score=risk&&risk.trust_score;
    const validScore=typeof score==='number'&&Number.isFinite(score)&&score>=0&&score<=100;
    $('#ip-score').textContent=validScore?String(score):'—';$('#score-unit').textContent=validScore?' / 100':'';
    $('#rating-state').textContent=validScore?(risk.ip!==ip?'网段评分':'第三方'):'暂无评分';
    $('#score-source').title=validScore?'Net.Coffee 信任评分，可能按网段缓存，并非 Claude 官方评分。':'第三方查询暂不可用，可点击前往原站查看。';
  }catch{
    if(ipQueryController!==controller)return;
    $('#rating-state').textContent='暂不可用';$('#ip-location').textContent='归属地暂无数据';$('#ip-score').textContent='—';$('#score-unit').textContent='';
  }finally{clearTimeout(timeout);}
}

function installAdvanced(kind){
  const button=$('#install-'+kind);if(button.disabled)return;
  button.disabled=true;button.textContent='安装中…';$('#'+kind+'-state').textContent='正在安装组件…';
  const timer=setTimeout(()=>{
    advancedTimers.delete(timer);button.textContent='已安装';$('#'+kind+'-state').textContent='已就绪';
    if(kind==='cloud')$('#open-cloud').disabled=false;
  },1800);advancedTimers.add(timer);
}
$('#install-mount').onclick=()=>installAdvanced('mount');
$('#install-cloud').onclick=()=>installAdvanced('cloud');
$('#open-cloud').onclick=()=>launchClient('cloud');
$('#close-mount').onclick=()=>$('#mount-dialog').close();
function appendMountPair(pair={local:'',remote:''}){
  const row=document.createElement('div');row.className='mount-pair';
  const heading=document.createElement('div');heading.className='mount-pair-heading';
  const title=document.createElement('span');title.className='mount-pair-number';
  const remove=document.createElement('button');remove.type='button';remove.className='text-button';remove.textContent='移除';remove.onclick=()=>{row.remove();numberMountPairs();};
  heading.append(title,remove);row.append(heading);
  for(const [key,label,placeholder] of [['local','本地目录','C:\\Users\\You\\Projects'],['remote','服务器目录','/home/workspace/projects']]){
    const field=document.createElement('label');field.textContent=label;
    const input=document.createElement('input');input.dataset.path=key;input.value=pair[key];input.placeholder=placeholder;input.required=true;input.spellcheck=false;input.autocomplete='off';field.append(input);row.append(field);
  }
  $('#mount-list').append(row);numberMountPairs();
}
function numberMountPairs(){
  $('#mount-list').querySelectorAll('.mount-pair').forEach((row,i)=>{row.querySelector('.mount-pair-number').textContent='目录 '+(i+1);row.querySelector('button').setAttribute('aria-label','移除目录 '+(i+1));});
  $('#mount-count').textContent=$('#mount-list').children.length+' 组目录';
}
$('#mount-settings').onclick=()=>{
  $('#mount-list').replaceChildren();(mountPairs.length?mountPairs:[{local:'',remote:''}]).forEach(appendMountPair);$('#mount-dialog').showModal();
};
$('#add-mount').onclick=()=>{appendMountPair();$('#mount-list').lastElementChild.querySelector('input').focus();};
$('#mount-form').onsubmit=e=>{
  e.preventDefault();mountPairs=[...$('#mount-list').querySelectorAll('.mount-pair')].map(row=>({local:row.querySelector('[data-path="local"]').value.trim(),remote:row.querySelector('[data-path="remote"]').value.trim()}));
  $('#mount-summary').textContent=mountPairs.length?'已设置 '+mountPairs.length+' 组本地与服务器目录。':'将本机目录挂载到服务器，支持多组目录。';$('#mount-dialog').close();
};

function updateLocalePreview(){
  const key=$('#locale-region').value;
  const preset=localePresets[key==='auto'?detectedRegion:key];
  $('#locale-timezone').textContent=preset?.timezone||'待选择';
  $('#locale-language').textContent=preset?.language||'待选择';
  $('#locale-code').textContent=preset?.locale||'待选择';
  $('#locale-hint').textContent=preset?'中文输入法不受系统语言影响。':'未能确定时区，请手动选择所在地。';
  $('#localization-form button[type="submit"]').disabled=!preset;
}
function openLocalization(){ $('#locale-region').value=localeSelection;updateLocalePreview();$('#localization-dialog').showModal(); }
$('#locale-region').onchange=updateLocalePreview;
$('#close-localization').onclick=()=>$('#localization-dialog').close();
$('#localization-form').onsubmit=e=>{
  e.preventDefault();localeSelection=$('#locale-region').value;states[1]='pending';taskLogs[1]='';selectedTasks.delete(1);renderTasks();$('#localization-dialog').close();
};
function openIpPolicy(change=false){
  $('#ip-policy-title').textContent=change?'更换入口与出口 IP':'出入口 IP 锁定设置';
  $('#ingress-ip-input').value=serverConnection.ingressIp;$('#egress-ip-input').value=serverConnection.egressIp;
  $('#lock-ingress').checked=ipPolicy.lockIngress;$('#lock-egress').checked=ipPolicy.lockEgress;
  for(const id of ['ingress-ip-input','egress-ip-input'])$('#'+id).setCustomValidity('');
  $('#ip-policy-dialog').showModal();
}
$('#lock-ips').onclick=()=>openIpPolicy();$('#change-ips').onclick=()=>openIpPolicy(true);
$('#close-ip-policy').onclick=()=>$('#ip-policy-dialog').close();
for(const id of ['ingress-ip-input','egress-ip-input'])$('#'+id).oninput=()=>$('#'+id).setCustomValidity('');
$('#ip-policy-form').onsubmit=e=>{
  e.preventDefault();
  for(const id of ['ingress-ip-input','egress-ip-input']){
    const field=$('#'+id);const pieces=field.value.trim().split('.');
    if(pieces.length!==4||pieces.some(n=>!/^\d{1,3}$/.test(n)||Number(n)>255)){field.setCustomValidity('请输入有效的 IPv4 地址。');field.reportValidity();return;}
  }
  const oldExit=serverConnection.egressIp;
  serverConnection={ingressIp:$('#ingress-ip-input').value.trim(),egressIp:$('#egress-ip-input').value.trim()};
  ipPolicy={lockIngress:$('#lock-ingress').checked,lockEgress:$('#lock-egress').checked};
  $('#server-ingress').textContent=serverConnection.ingressIp;$('#server-address').textContent=serverConnection.egressIp;
  $('#ip-policy-state').textContent=ipPolicy.lockIngress&&ipPolicy.lockEgress?'已锁定':ipPolicy.lockIngress||ipPolicy.lockEgress?'部分锁定':'未锁定';
  $('#ip-policy-summary').textContent=ipPolicy.lockEgress?'入口接入，指定住宅 IP 出站。':'入口用于接入，住宅 IP 用于出站。';
  $('#ip-policy-dialog').close();
  if(oldExit!==serverConnection.egressIp){states[1]='pending';taskLogs[1]='';localeSelection='auto';detectedRegion=null;renderTasks();queryIpInfo(serverConnection.egressIp);}
};

// 项目与社区链接。VPS 推广入口单独标注 sponsored。
const resourceLinks = {
  vps: 'https://www.rainyun.com/Agentom_',
  qq: 'https://qm.qq.com/q/cyUWAwYKQg',
  github: 'https://github.com/Muchuaner/safe-code'
};
for(const link of document.querySelectorAll('[data-resource]')){
  const url=resourceLinks[link.dataset.resource];
  if(url){link.href=url;link.target='_blank';link.rel='noopener noreferrer'+(link.dataset.resource==='vps'?' sponsored':'');link.referrerPolicy='no-referrer';link.removeAttribute('aria-disabled');link.title=link.textContent.trim();}
}
