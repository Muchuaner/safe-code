const $ = selector => document.querySelector(selector);
const appWindow = $('#window');
let connectionTimer;
let ipQueryController;
let serverConnection = { ingressIp: '', egressIp: '' };
let ipPolicy = { lockIngress: false, lockEgress: false };
let localeSelection = '';
let accountSettingsSaved = false;
let localeSaving = false;
const localePresets = {
  jp: {name:'日本',timezone:'Asia/Tokyo',language:'日语',locale:'ja_JP.UTF-8'},
  us_ny: {name:'美国 · 纽约',timezone:'America/New_York',language:'英语（美国）',locale:'en_US.UTF-8'},
  us_la: {name:'美国 · 洛杉矶',timezone:'America/Los_Angeles',language:'英语（美国）',locale:'en_US.UTF-8'},
  sg: {name:'新加坡',timezone:'Asia/Singapore',language:'英语（新加坡）',locale:'en_SG.UTF-8'},
  gb: {name:'英国',timezone:'Europe/London',language:'英语（英国）',locale:'en_GB.UTF-8'},
  de: {name:'德国',timezone:'Europe/Berlin',language:'德语',locale:'de_DE.UTF-8'},
  de_fra: {name:'德国 · 法兰克福',timezone:'Europe/Berlin',language:'德语',locale:'de_DE.UTF-8'},
  kr: {name:'韩国',timezone:'Asia/Seoul',language:'韩语',locale:'ko_KR.UTF-8'}
};
function currentLocale(){return Object.hasOwn(localePresets,localeSelection)?localePresets[localeSelection]:null;}
function deploymentSettingsReady(){return accountSettingsSaved&&currentLocale()!==null;}
function resetDeploymentSettings(){accountSettingsSaved=false;localeSelection='';accountDraftPending=false;}
let runToken = 0;
let busy = false;
let currentMode = 'deployment';
let mountPairs = [];
const advancedTimers = new Set();
let selecting = false;
let selectedTasks = new Set();
let states = [];
let taskLogs = [];
let accountDraftPending=false;
let inputManaged=false;
let rebootRequired=false;
let removeChineseInput;
const tasks = [
  ['更新系统并设置工作用户', '先等待系统更新、更新软件包，再设置工作用户和机器名称。'],
  ['本土化设置', '点击「自定义」选择所在地、时区与系统语言。'],
  ['安装虚拟桌面', '配置 XFCE 与远程桌面，保留断线后的工作状态。'],
  ['安装 Chrome', '安装 Chrome 浏览器，配置桌面启动入口。'],
  ['安装中文输入法', '按需安装 Fcitx5 拼音输入法，支持卸载。'],
  ['安装 Claude Code', '准备 Claude Code、Git 和 tmux 开发工具。'],
  ['配置 SSH 安全通道', '通过加密隧道连接终端与远程桌面。']
];
states = Array(tasks.length).fill('pending');
taskLogs = Array(tasks.length).fill('');
function showScreen(name) {
  for(const id of ['notice','login','workspace','api']) $('#'+id+'-screen').hidden = id!==name;
  appWindow.classList.toggle('wide',name==='workspace');
  appWindow.classList.toggle('api-window',name==='api');
}
function stopWork(){for(const timer of advancedTimers)clearTimeout(timer);advancedTimers.clear();if(ipQueryController)ipQueryController.abort();clearTimeout(connectionTimer);runToken++;busy=false;}
function exit(){stopWork();$('#login-form').reset();appWindow.hidden=true;$('#restore-window').hidden=true;$('#ip-policy-dialog').close();$('#socks-dialog').close();$('#localization-dialog').close();$('#mount-dialog').close();$('#maintenance-result').close();$('#result-body').replaceChildren();$('#client-window').close();$('#client-frame').removeAttribute('src');document.title='Safe Code · 已退出';}
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
$('#reveal').onclick=()=>{const visible=$('#password').type==='password';$('#password').type=visible?'text':'password';setEye($('#reveal'),visible);$('#reveal').setAttribute('aria-label',visible?'隐藏密码':'显示密码');};
function resetLogin(){
  $('#login-form').querySelectorAll('input,button').forEach(el=>el.disabled=false);
  $('#back').disabled=false;$('#password').type='password';setEye($('#reveal'),false);$('#reveal').setAttribute('aria-label','显示密码');
  $('#connect').textContent='连接服务器';
  $('#connection-message').className='connection-message';$('#connection-message').textContent='';
}
function returnToServerLogin(){
  stopWork();
  serverConnection={ingressIp:'',egressIp:''};
  $('#server-ingress').textContent='';$('#server-address').textContent='';
  $('#update-login-password').hidden=true;$('#disconnect-server').hidden=true;
  $('#login-form').reset();
  $('#username').required=true;$('#password').required=true;
  resetLogin();showScreen('login');$('#ip').focus();
}
$('#disconnect-server').onclick=returnToServerLogin;
function isDeveloperHost(host){return ['127.0.0.1','localhost','::1','[::1]'].includes(host.toLowerCase());}
function enterDeveloperPreview(){
  stopWork();serverConnection={ingressIp:'127.0.0.1',egressIp:'127.0.0.1'};
  $('#password').value='';$('#server-ingress').textContent='127.0.0.1';$('#server-address').textContent='127.0.0.1';$('#update-login-password').hidden=true;$('#disconnect-server').hidden=true;
  states=Array(tasks.length).fill('pending');taskLogs=Array(tasks.length).fill('');resetDeploymentSettings();
  $('#rating-state').textContent='示例数据';$('#ip-location').textContent='开发预览';$('#ip-score').textContent='—';$('#score-unit').textContent='';
  let banner=$('#developer-preview');if(!banner){banner=document.createElement('div');banner.id='developer-preview';banner.className='developer-preview';$('.window>.titlebar').insertBefore(banner,$('.window-controls'));}
  banner.replaceChildren(document.createTextNode('开发预览 · 未连接服务器 '));
  const back=document.createElement('button');back.className='text-button';back.textContent='返回登录';back.onclick=()=>location.reload();banner.append(back);
  renderTasks();resetLogin();switchMode('deployment');showScreen('workspace');
}
// 预览地址不需要填写用户名或密码。
$('#ip').addEventListener('input',()=>{const preview=isDeveloperHost($('#ip').value.trim());$('#username').required=!preview;$('#password').required=!preview;});
$('#login-form').onsubmit=event=>{
  event.preventDefault();if(window.__safeCodeDesktop){showAppMessage('客户端核心模块未就绪，无法连接。');return;}const ip=$('#ip').value.trim();const username=$('#username').value.trim();
  if(isDeveloperHost(ip)){enterDeveloperPreview();return;}
  if(!ip || !username){$('#connection-message').textContent='请填写 IP 地址和用户名。';$('#connection-message').className='connection-message error';return;}
  $('#login-form').querySelectorAll('input,button').forEach(el=>el.disabled=true);$('#back').disabled=true;
  $('#connect').textContent='正在连接…';$('#connection-message').textContent='正在建立 SSH 连接，请稍候…';$('#connection-message').className='connection-message connecting';
  connectionTimer=setTimeout(()=>{
    serverConnection = { ingressIp: ip, egressIp: ip };
    $('#password').value='';$('#server-ingress').textContent=serverConnection.ingressIp;$('#server-address').textContent=serverConnection.egressIp;$('#disconnect-server').hidden=false;
      states=Array(tasks.length).fill('pending');taskLogs=Array(tasks.length).fill('');resetDeploymentSettings();selecting=false;selectedTasks.clear();renderTasks();resetLogin();switchMode('deployment');$('#maintenance-result').close();showScreen('workspace');queryIpInfo(serverConnection.egressIp);
  },1400);
};
function renderTasks(){
  const ready=deploymentSettingsReady();
  const preset=currentLocale();
  $('#steps').replaceChildren(...tasks.map(([title,description],i)=>{
    const row=document.createElement('article');row.className='step '+states[i];
    const number=document.createElement('span');number.className='step-number';number.textContent=String(i+1);
    const copy=document.createElement('div');copy.className='step-copy';
    const heading=document.createElement('h3');heading.textContent=title;
    const desc=document.createElement('p');desc.className=taskLogs[i]?'task-log':'';const detail=i===1&&preset?preset.name+' · '+preset.timezone+' · '+preset.locale:description;desc.textContent=taskLogs[i]||detail;desc.setAttribute('role','status');desc.setAttribute('aria-live','polite');copy.append(heading,desc);
    const button=document.createElement('button');button.className='deploy-button';button.textContent=i===0&&accountDraftPending&&states[i]==='done'?'更新':states[i]==='done'?'已完成':states[i]==='running'?'部署中…':'开始部署';button.disabled=busy||!ready||!canDeployTask(i);button.setAttribute('aria-label',title+'：'+button.textContent);button.onclick=()=>deployOne(i);
    if(i===4&&inputManaged&&!selecting&&states[i]!=='running'){
      button.classList.add('input-remove-button');button.textContent='卸载';button.disabled=busy;
      button.setAttribute('aria-label','卸载中文输入法');button.onclick=()=>removeChineseInput();
    }
    if(selecting){
      const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.className='task-checkbox';checkbox.disabled=states[i]==='done'||busy||!ready;checkbox.checked=states[i]!=='done'&&selectedTasks.has(i);checkbox.setAttribute('aria-label','选择'+title);
      checkbox.onchange=()=>{if(checkbox.checked)selectedTasks.add(i);else selectedTasks.delete(i);updateBatchControls();};
      row.append(checkbox);if(states[i]==='done')row.classList.add('unselectable');
    }
    if(i===0||i===1){const settings=document.createElement('button');settings.className='deploy-button locale-settings-button';settings.textContent='自定义';settings.setAttribute('aria-label',i===0?'自定义工作用户和机器名称':'自定义本土化');settings.disabled=busy||(window.__safeCodeDesktop&&!!$('#developer-preview'));settings.onclick=i===1?openLocalization:()=>openAccountSettings();row.append(number,copy,settings,button);}else row.append(number,copy,button);return row;
  }));
  const completed=states.filter(state=>state==='done').length;
  $('#overall-status').textContent=completed===tasks.length?'全部配置完成':completed+' / '+tasks.length+' 已完成';
  const finished=completed===tasks.length;
  $('#overall-detail').textContent=!ready&&(!finished||accountDraftPending)?'请先分别打开前两项「自定义」并保存设置。':finished?'开发环境已就绪。':'可单项部署，或选择所需项目批量部署。';
  $('#deploy-all').disabled=busy||!ready||finished;$('#deploy-all').textContent=finished?'部署完成':busy?'正在部署…':'一键部署';
  $('#reboot-required').hidden=!rebootRequired;
  updateBatchControls();
}
function canDeployTask(index){return states[index]!=='done'||(index===0&&accountDraftPending);}
function updateBatchControls(){
  $('#cancel-selection').hidden=!selecting;
  $('.deployment-heading > span').textContent=selecting?'勾选要部署的项目，已完成项不可选。':'按步骤部署，或一次完成。';
  if(selecting){
    $('#deploy-all').textContent='开始部署（'+selectedTasks.size+' 项）';
    $('#deploy-all').disabled=busy||!deploymentSettingsReady()||selectedTasks.size===0;
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
  if(window.__safeCodeDesktop)throw new Error('桌面客户端不允许使用模拟部署。');
  if(!deploymentSettingsReady())return false;
  states[index]='running';
  const preset=currentLocale();
  const messages=index===1?['读取已保存的本土化设置…','设置时区为 '+preset.timezone+'…','生成 '+preset.locale+' 语言环境…','应用'+preset.language+'系统语言与时间格式…','本土化设置完成：'+preset.timezone+' / '+preset.locale]:deploymentLogs[index];
  for(let i=0;i<messages.length;i++){
    if(token!==runToken)return false;
    if(i===messages.length-1)states[index]='done';
    updateTaskLog(index,messages[i]);
    if(i<messages.length-1)await new Promise(resolve=>setTimeout(resolve,1100));
  }
  return true;
}
async function deployOne(index){if(busy||!deploymentSettingsReady()||!canDeployTask(index))return;selecting=false;selectedTasks.clear();busy=true;const token=++runToken;await simulateTask(index,token);if(token!==runToken)return;busy=false;renderTasks();}
$('#deploy-all').onclick=async()=>{
  if(busy||!deploymentSettingsReady())return;
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
    $('#rating-state').textContent='';$('#ip-location').textContent='美国 · 纽约';$('#ip-score').textContent='85';$('#score-unit').textContent=' / 100';
    $('#score-source').title='当前为示例 IP，归属地与评分仅为界面示意；真实查询暂支持公网 IPv4。';return;
  }
  const controller=new AbortController();ipQueryController=controller;
  const timeout=setTimeout(()=>controller.abort(),35000);
  $('#rating-state').textContent='查询中';$('#ip-location').textContent='正在查询…';$('#ip-score').textContent='—';$('#score-unit').textContent='';
  try{
    const response=await fetch('https://ip.net.coffee/api/ip/lookup/'+encodeURIComponent(ip),{signal:controller.signal,credentials:'omit',referrerPolicy:'no-referrer'});
    if(!response.ok)throw new Error('查询失败');const data=await response.json();if(ipQueryController!==controller)return;
    const geo=selectGeoConsensus(data);const countries={'Japan':'日本','United States':'美国','Hong Kong':'香港','Singapore':'新加坡','Germany':'德国','United Kingdom':'英国','South Korea':'韩国'};const cities={'Tokyo':'东京','New York':'纽约','Los Angeles':'洛杉矶','Singapore':'新加坡'};
    $('#ip-location').textContent=[countries[geo.country]||geo.country,cities[geo.city]||geo.city].filter(Boolean).join(' · ')||'定位待确认';
    const score=data.trust_score;const valid=typeof score==='number'&&Number.isFinite(score)&&score>=0&&score<=100;
    $('#ip-score').textContent=valid?String(score):'—';$('#score-unit').textContent=valid?' / 100':'';$('#rating-state').textContent=geo.source;
    $('#score-source').title='定位优先采用 Net.Coffee 多源综合结果；评分可能按网段缓存，并非 Claude 官方评分。';
  }catch{if(ipQueryController!==controller)return;$('#rating-state').textContent='暂不可用';$('#ip-location').textContent='暂无归属地';$('#ip-score').textContent='—';$('#score-unit').textContent='';}
  finally{clearTimeout(timeout);}
}
function selectGeoConsensus(data){
  let result={country:data.country||'',code:(data.countryCode||'').toLowerCase(),city:data.city||'',source:'简表'};
  const sources=(data.geo_sources||[]).filter(x=>x.country_code);const groups=new Map();for(const source of sources){const code=source.country_code.toLowerCase();if(!groups.has(code))groups.set(code,[]);groups.get(code).push(source);}
  const sorted=[...groups].sort((a,b)=>b[1].length-a[1].length);
  if(sorted.length>1&&sorted[0][1].length===sorted[1][1].length)return{country:'定位待确认',code:'',city:'',source:'来源有分歧'};
  if(sorted.length){const[code,items]=sorted[0];const counts=new Map();for(const x of items)if(x.city)counts.set(x.city,(counts.get(x.city)||0)+1);const cities=[...counts].sort((a,b)=>b[1]-a[1]||a[0].length-b[0].length);result={country:items[0].country||'',code,city:cities[0]?.[0]||items[0].region||'',source:'多源综合'};}
  return result;
}

function installAdvanced(kind){
  if(window.__safeCodeDesktop)throw new Error('桌面客户端不允许使用模拟安装。');
  const button=$('#install-'+kind);if(button.disabled)return;
  button.disabled=true;button.textContent='安装中…';$('#'+kind+'-state').textContent='正在安装组件…';
  const timer=setTimeout(()=>{
    advancedTimers.delete(timer);button.textContent='已安装';$('#'+kind+'-state').textContent='已就绪';
    if(kind==='cloud'){$('#open-cloud').disabled=false;if($('#open-cloud-remote'))$('#open-cloud-remote').disabled=false;}
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
  const remove=document.createElement('button');remove.type='button';remove.className='text-button mount-remove';remove.textContent='移除';remove.onclick=()=>{row.remove();numberMountPairs();};
  heading.append(title,remove);row.append(heading);
  for(const [key,label,placeholder] of [['local','本地目录','C:\\Users\\You\\Projects'],['remote','服务器目录','/home/workspace/projects']]){
    const field=document.createElement('label');field.textContent=label;
    const input=document.createElement('input');input.dataset.path=key;input.value=pair[key];input.placeholder=placeholder;input.required=true;input.spellcheck=false;input.autocomplete='off';field.append(input);row.append(field);
  }
  $('#mount-list').append(row);numberMountPairs();
}
function numberMountPairs(){
  $('#mount-list').querySelectorAll('.mount-pair').forEach((row,i)=>{row.querySelector('.mount-pair-number').textContent='目录 '+(i+1);row.querySelector('.mount-remove').setAttribute('aria-label','移除目录 '+(i+1));});
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
  const preset=Object.hasOwn(localePresets,key)?localePresets[key]:null;
  $('#locale-timezone').textContent=preset?.timezone||'待选择';
  $('#locale-language').textContent=preset?.language||'待选择';
  $('#locale-code').textContent=preset?.locale||'待选择';
  $('#locale-hint').textContent=preset?'保存后需部署才会应用。':'请先选择所在地。';
  $('#localization-form button[type="submit"]').disabled=localeSaving||!preset;
}
function openLocalization(){ $('#locale-region').value=localeSelection;updateLocalePreview();$('#localization-dialog').showModal(); }
function saveLocaleChoice(region){
  if(localeSelection!==region){states[1]='pending';taskLogs[1]='';selectedTasks.delete(1);}
  localeSelection=region;
  renderTasks();
  $('#localization-dialog').close();
}
$('#locale-region').onchange=updateLocalePreview;
$('#close-localization').onclick=()=>{if(!localeSaving)$('#localization-dialog').close();};
$('#localization-dialog').addEventListener('cancel',event=>{if(localeSaving)event.preventDefault();});
$('#localization-form').onsubmit=e=>{
  e.preventDefault();const region=$('#locale-region').value;
  if(Object.hasOwn(localePresets,region))saveLocaleChoice(region);
};
const ipPickerControls=new Map();
function closeIpPickers(){for(const {trigger,menu} of ipPickerControls.values()){menu.hidden=true;trigger.setAttribute('aria-expanded','false');}}
function syncIpPickers(){
  for(const [id,{trigger,value,menu}] of ipPickerControls){
    const select=document.getElementById(id);value.textContent=select.value||'未检测到可用 IP';trigger.disabled=select.options.length===0;trigger.classList.toggle('is-empty',!select.value);
    for(const option of menu.querySelectorAll('[role=option]')){const selected=option.dataset.ip===select.value;option.setAttribute('aria-selected',String(selected));}
  }
  const button=$('#ip-policy-form [type=submit]');if(!$('#ip-policy-form').hasAttribute('aria-busy'))button.disabled=[...ipPickerControls.keys()].some(id=>!document.getElementById(id).value);
}
function setDetectedIpOptions(addresses){
  const unique=[...new Set(addresses)];
  for(const [id,{select,menu}] of ipPickerControls){
    const previous=select.value;select.replaceChildren();menu.replaceChildren();
    for(const ip of unique){
      const option=document.createElement('option');option.value=ip;option.textContent=ip;select.append(option);
      const item=document.createElement('button');item.type='button';item.className='ip-picker-option';item.setAttribute('role','option');item.tabIndex=-1;item.dataset.ip=ip;
      const text=document.createElement('span');text.textContent=ip;const check=document.createElementNS('http://www.w3.org/2000/svg','svg');check.setAttribute('viewBox','0 0 16 16');check.setAttribute('aria-hidden','true');const path=document.createElementNS(check.namespaceURI,'path');path.setAttribute('d','m3 8 3 3 7-7');check.append(path);item.append(text,check);
      item.onclick=()=>{select.value=ip;select.dispatchEvent(new Event('change'));closeIpPickers();ipPickerControls.get(id).trigger.focus();};menu.append(item);
    }
    select.value=unique.includes(previous)?previous:(unique[0]||'');
  }
  syncIpPickers();
}
for(const holder of document.querySelectorAll('[data-ip-picker]')){
  const id=holder.dataset.ipPicker,select=document.getElementById(id),prefix=id.replace('-input','');
  const trigger=document.createElement('button');trigger.type='button';trigger.className='ip-picker-trigger';trigger.id=prefix+'-trigger';trigger.setAttribute('aria-haspopup','listbox');trigger.setAttribute('aria-expanded','false');trigger.setAttribute('aria-labelledby',prefix+'-label '+prefix+'-value');trigger.setAttribute('aria-controls',prefix+'-options');
  const value=document.createElement('span');value.id=prefix+'-value';
  const icon=document.createElementNS('http://www.w3.org/2000/svg','svg');icon.setAttribute('viewBox','0 0 16 16');icon.setAttribute('aria-hidden','true');const path=document.createElementNS(icon.namespaceURI,'path');path.setAttribute('d','m4 6 4 4 4-4');icon.append(path);trigger.append(value,icon);
  const menu=document.createElement('div');menu.id=prefix+'-options';menu.className='ip-picker-menu';menu.setAttribute('role','listbox');menu.setAttribute('aria-labelledby',prefix+'-label');menu.hidden=true;holder.append(trigger,menu);
  const open=()=>{const wasOpen=!menu.hidden;closeIpPickers();if(wasOpen)return;menu.hidden=false;trigger.setAttribute('aria-expanded','true');(menu.querySelector('[aria-selected=true]')||menu.firstElementChild)?.focus();};
  trigger.onclick=open;select.onchange=()=>{select.setCustomValidity('');syncIpPickers();};
  holder.onkeydown=event=>{
    if(event.key==='Escape'&&!menu.hidden){event.preventDefault();event.stopPropagation();closeIpPickers();trigger.focus();return;}
    if(event.key==='Tab'){closeIpPickers();return;}
    if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){
      event.preventDefault();if(menu.hidden){open();return;}
      const options=[...menu.querySelectorAll('[role=option]')],index=options.indexOf(document.activeElement);if(!options.length)return;
      const next=event.key==='Home'?0:event.key==='End'?options.length-1:(index+(event.key==='ArrowUp'?-1:1)+options.length)%options.length;options[next].focus();
    }
  };
  ipPickerControls.set(id,{select,trigger,value,menu});
}
document.addEventListener('pointerdown',event=>{if(!event.target.closest('.ip-picker'))closeIpPickers();});
$('#ip-policy-dialog').addEventListener('close',closeIpPickers);
syncIpPickers();
function openIpPolicy(){
  $('#ip-policy-title').textContent='更换入口与出口 IP';
  $('#ingress-ip-input').value=serverConnection.ingressIp;$('#egress-ip-input').value=serverConnection.egressIp;
  $('#lock-ingress').checked=ipPolicy.lockIngress;$('#lock-egress').checked=ipPolicy.lockEgress;
  for(const id of ['ingress-ip-input','egress-ip-input'])$('#'+id).setCustomValidity('');
  syncIpPickers();$('#ip-policy-dialog').showModal();
}
$('#change-ips').onclick=()=>openIpPolicy();
$('#close-ip-policy').onclick=()=>$('#ip-policy-dialog').close();
$('#socks-experimental').onclick=()=>$('#socks-dialog').showModal();
$('#socks-close').onclick=()=>$('#socks-dialog').close();
$('#socks-form').onsubmit=event=>event.preventDefault();
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
  if(oldExit!==serverConnection.egressIp)queryIpInfo(serverConnection.egressIp);
};

// 项目与社区链接。VPS 推广入口单独标注 sponsored。
const resourceLinks = {
  vps: 'https://www.rainyun.com/Agentom_',
  qq: 'https://qm.qq.com/q/Bzd5aqxW36',
  github: 'https://github.com/Muchuaner/safe-code'
};
for(const link of document.querySelectorAll('[data-resource]')){
  const url=resourceLinks[link.dataset.resource];
  if(url){link.href=url;link.target='_blank';link.rel='noopener noreferrer'+(link.dataset.resource==='vps'?' sponsored':'');link.referrerPolicy='no-referrer';link.removeAttribute('aria-disabled');link.title=link.textContent.trim();}
}

function setEye(button,visible){button.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>'+(visible?'':'<path d="m3 3 18 18"/>')+'</svg>';button.setAttribute('aria-pressed',String(visible));}
setEye($('#reveal'),false);
document.querySelectorAll('[data-reveal]').forEach(button=>{setEye(button,false);button.onclick=()=>{const field=document.getElementById(button.dataset.reveal);const visible=field.type==='password';field.type=visible?'text':'password';setEye(button,visible);};});
function openAccountSettings(){$('#account-settings').showModal();}
$('#account-settings-form').onsubmit=e=>{e.preventDefault();if(!e.currentTarget.reportValidity())return;accountSettingsSaved=true;$('#account-settings').close();states[0]='pending';renderTasks();};
for(const id of ['server-ingress','server-address']){const value=document.getElementById(id);const eye=document.createElement('button');eye.className='ip-eye reveal';eye.type='button';eye.setAttribute('aria-label','隐藏'+(id==='server-ingress'?'入口':'出口')+' IP');setEye(eye,true);let visible=true;let actual='';eye.onclick=()=>{if(visible)actual=value.textContent;visible=!visible;value.textContent=visible?actual:'•••.•••.•••.•••';eye.setAttribute('aria-label',(visible?'隐藏':'显示')+' IP');setEye(eye,visible);};new MutationObserver(()=>{if(!visible&&value.textContent!=='•••.•••.•••.•••'){actual=value.textContent;value.textContent='•••.•••.•••.•••';}}).observe(value,{childList:true});value.after(eye);}
for(const button of document.querySelectorAll('[data-open-services]'))button.onclick=()=>$('#services-dialog').showModal();
for(const id of ['usage-consent','settings-usage-consent'])document.getElementById(id).onchange=e=>{const enabled=e.target.checked;$('#usage-consent').checked=enabled;$('#settings-usage-consent').checked=enabled;};
$('#feedback-form').onsubmit=e=>{e.preventDefault();$('#feedback-status').textContent='请在 Windows 客户端内提交反馈。';};
$('#check-update').onclick=()=>{$('#update-status').textContent='请在 Windows 客户端内检查更新。';};

let appDialogQueue=Promise.resolve();
function appDialog(message,options={}){
  const next=appDialogQueue.then(()=>new Promise(resolve=>{
    if(options.showIf&&!options.showIf()){resolve(false);return;}
    const dialog=$('#app-message-dialog'),body=$('#app-message-body');
    $('#app-message-title').textContent=options.title||'提示';body.textContent=message;
    body.style.whiteSpace=options.preserveWhitespace?'pre-wrap':'';
    $('#app-message-details').hidden=!options.details;$('#app-message-details').open=false;$('#app-message-details').ontoggle=()=>{$('#app-message-details summary').textContent=$('#app-message-details').open?'收起服务器指纹':'查看服务器指纹';};$('#app-message-detail-text').textContent=options.details||'';
    const yes=$('#app-message-confirm'),no=$('#app-message-cancel');yes.textContent=options.confirm||'知道了';no.textContent=options.cancel||'取消';no.hidden=!options.cancel;
    yes.onclick=()=>dialog.close('confirm');no.onclick=()=>dialog.close('cancel');dialog.querySelector('[data-dialog-dismiss]').onclick=()=>dialog.close('cancel');
    dialog.addEventListener('close',()=>{
      if(options.clearOnClose)body.textContent='';
      resolve(dialog.returnValue==='confirm');
    },{once:true});dialog.returnValue='';dialog.showModal();(options.cancel?no:yes).focus();
  }));appDialogQueue=next.catch(()=>{});return next;
}
function showAppMessage(message,options={}){return appDialog(String(message),options);}

function initCloudRemoteDialog(){
  const dialog=$('#cloud-remote-dialog');
  if(!dialog)return;
  const warningView=$('#cloud-remote-warning-view');
  const logView=$('#cloud-remote-log-view');
  const successView=$('#cloud-remote-success-view');
  const closeBtn=$('#close-cloud-remote');
  const cancelBtn=$('#cloud-remote-cancel');
  const confirmBtn=$('#cloud-remote-confirm');
  const copyBtn=$('#cloud-remote-copy-btn');
  const doneBtn=$('#cloud-remote-done-btn');
  const toggleLogBtn=$('#cloud-remote-toggle-log');
  const urlLink=$('#cloud-remote-url-link');
  const logOutput=$('#cloud-remote-log-output');

  function showView(view){
    warningView.hidden=view!=='warning';
    logView.hidden=view!=='log';
    successView.hidden=view!=='success';
  }

  window.openCloudRemoteDialog=function(initialUrl){
    if(initialUrl){showSuccess(initialUrl);}
    else{showView('warning');}
    dialog.showModal();
  };

  function copyUrl(){
    const url=urlLink.href||urlLink.textContent;
    if(!url)return;
    const finish=()=>{const orig=copyBtn.textContent;copyBtn.textContent='已复制 √';setTimeout(()=>{copyBtn.textContent=orig;},2000);};
    if(navigator.clipboard?.writeText){navigator.clipboard.writeText(url).then(finish).catch(()=>{const i=document.createElement('input');i.value=url;document.body.appendChild(i);i.select();document.execCommand('copy');i.remove();finish();});}
    else{const i=document.createElement('input');i.value=url;document.body.appendChild(i);i.select();document.execCommand('copy');i.remove();finish();}
  }

  function showSuccess(url){
    showView('success');
    urlLink.href=url;
    urlLink.textContent=url;
    const qrContainer=$('#cloud-remote-qrcode');
    qrContainer.innerHTML='';
    if(typeof qrcode!=='undefined'){
      try{
        const qr=qrcode(0,'M');
        qr.addData(url);
        qr.make();
        qrContainer.innerHTML=qr.createSvgTag({cellSize:4,margin:8,scalable:true});
        const svg=qrContainer.querySelector('svg');
        if(svg){svg.style.width='160px';svg.style.height='160px';svg.style.display='block';}
      }catch(e){qrContainer.textContent='二维码生成失败';}
    }
  }

  window.showCloudRemoteSuccess=showSuccess;
  closeBtn.onclick=()=>dialog.close();
  cancelBtn.onclick=()=>dialog.close();
  doneBtn.onclick=()=>dialog.close();
  copyBtn.onclick=copyUrl;

  dialog.addEventListener('cancel',()=>{onGuideConfirm=null;});
  dialog.addEventListener('keydown',e=>{
    if((e.ctrlKey||e.metaKey)&&(e.key==='c'||e.key==='C')){
      const sel=window.getSelection().toString();
      if(!sel&&!successView.hidden){e.preventDefault();copyUrl();}
    }
  });

  toggleLogBtn.onclick=()=>{
    if(logView.hidden){logView.hidden=false;toggleLogBtn.textContent='隐藏配置日志';}
    else{logView.hidden=true;toggleLogBtn.textContent='查看配置日志';}
  };

  if(!window.__safeCodeDesktop){
    confirmBtn.onclick=async()=>{
      showView('log');
      logOutput.textContent='已确认风险，准备配置公网访问…\n';
      const steps=[
        '正在检查 CloudCLI 安装状态…',
        '正在配置 CloudCLI 允许公网访问 (0.0.0.0:3001)…',
        '正在重启 CloudCLI 服务以应用配置…',
        '正在检查防火墙配置…',
        '正在获取公网访问地址…',
        'CloudCLI 已成功开放公网访问：http://203.0.113.42:3001',
        '提示：请确保网页密码安全。'
      ];
      for(const line of steps){
        await new Promise(r=>setTimeout(r,350));
        logOutput.textContent+=line+'\n';
        logOutput.scrollTop=logOutput.scrollHeight;
      }
      await new Promise(r=>setTimeout(r,400));
      showSuccess('http://203.0.113.42:3001');
    };
    $('#open-cloud-remote').onclick=()=>window.openCloudRemoteDialog();
  }
}
initCloudRemoteDialog();

if(!window.__safeCodeDesktop)showScreen('notice');

// 登录信息教程也可在普通浏览器预览。
$('#login-help').onclick=()=>$('#login-help-dialog').showModal();
$('#advanced-connect').onclick=()=>$('#advanced-dialog').showModal();
$('#advanced-form').onsubmit=event=>{event.preventDefault();$('#advanced-dialog').close();};
$('#enter-preview').onclick=()=>{
  if(window.__safeCodeDesktop){showAppMessage('客户端核心模块未就绪，无法进入预览。');return;}
  $('#advanced-dialog').close();enterDeveloperPreview();
};
