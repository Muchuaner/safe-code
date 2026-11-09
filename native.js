/* Windows 客户端桥接。普通浏览器继续使用概念稿；桌面模式不执行模拟部署。 */
if (window.chrome?.webview && location.host === 'safe-code.local') {
  (() => {
    const pending = new Map();
    let currentTask = -1;
    let nativeServer = null;
    let sshPort = 22;
    let metricsTask = Promise.resolve();
    let metricsTimer=null;let metricsBusy=false;
    let accountOptions={};
    const originalRenderTasks=renderTasks;renderTasks=function(){originalRenderTasks();syncAdvancedButtons();};
    const ids = ['account','localization','desktop','chrome','input','claude','ssh'];
    const native = (op,args={}) => new Promise((resolve,reject) => {
      const id=crypto.randomUUID();pending.set(id,{resolve,reject});window.chrome.webview.postMessage({id,op,args});
    });
    window.safeCodeNativeResponse = ({id,result,error}) => {
      const item=pending.get(id);if(!item)return;pending.delete(id);error?item.reject(new Error(error)):item.resolve(result);
    };
    window.safeCodeNativeLog = (step,line) => {
      if(step==='mount_apply'&&mountSaving){mountProgressText.textContent=line;}
      else if(step==='folder'&&$('#remote-folder-dialog').open){appendFolderLog(line);}
      else if(currentTask>=0){taskLogs[currentTask]=line;renderTasks();}
      else if(connectionInFlight&&!$('#workspace-screen').hidden){connectionProgress(line);}
      else if($('#login-screen').hidden===false){$('#connection-message').textContent=line;}
      else if(advancedOperation&&!advancedOperation.done&&step===advancedOperation.key){appendOperationLog(line);}
    };
    window.safeCodeNativeLogs=lines=>{for(const entry of lines)window.safeCodeNativeLog(entry.step,entry.line);};
    const nativeStyle=document.createElement('style');nativeStyle.textContent=`body{padding:0;display:block;background:white;min-height:100vh}.window,.window.wide,.window.maximized{width:100%;height:100vh;max-width:none;border:0;border-radius:0;box-shadow:none;transition:none}.header-controls{width:112px}.maintenance-panel .advanced-actions>span{overflow:hidden;white-space:nowrap;text-overflow:ellipsis;max-width:100%}.connection-message{overflow:hidden;white-space:nowrap;text-overflow:ellipsis}#login-screen{padding-top:23px}.login-heading{margin-top:0;margin-bottom:14px}#login-form input:not([type=checkbox]){height:36px;margin-bottom:12px}#login-form .password-wrap input{margin-bottom:0}#login-form .connection-message{height:27px;padding-top:8px}#login-form .primary{height:36px}#login-screen>.back{margin-top:10px}.native-log-note{white-space:normal;line-height:1.6;color:#99a599;font-size:9px}`;document.head.append(nativeStyle);
    $('#minimize').onclick=()=>native('window.minimize');
    $('#maximize').onclick=()=>native('window.maximize');
    $('#close').onclick=()=>native('window.close');$('#exit').onclick=()=>native('window.close');
    $('.titlebar').addEventListener('mousedown',e=>{if(e.button===0&&!e.target.closest('button'))native('window.drag');});
    $('.titlebar').addEventListener('dblclick',e=>{if(!e.target.closest('button'))native('window.maximize');});
    const originalShowScreen=showScreen;showScreen=function(name){originalShowScreen(name);native('window.resize',{wide:name==='workspace'});};
    const advanced=document.createElement('button');advanced.type='button';advanced.className='plain';advanced.textContent='高级连接';advanced.style.marginTop='8px';
    advanced.onclick=()=>{$('#ssh-port').value=String(sshPort);$('#advanced-dialog').showModal();$('#ssh-port').focus();};
    $('#advanced-form').onsubmit=e=>{e.preventDefault();if(!$('#advanced-form').reportValidity())return;sshPort=Number($('#ssh-port').value);$('#advanced-dialog').close();};
    $('#login-form').append(advanced);
    function applyServer(server){
      nativeServer=server;states=ids.map(id=>server.statuses?.[id]?'done':'pending');
      if(currentTask>=0&&states[currentTask]!=='done')states[currentTask]='running';
      const desired=currentLocale();if(desired&&(server.timezone!==desired.timezone||server.systemLocale!==desired.locale))states[1]='pending';
      if(server.ipPolicy){ipPolicy=server.ipPolicy;$('#ip-policy-state').textContent=(ipPolicy.lockIngress||ipPolicy.lockEgress)?(server.ipPolicyHealthy?'已锁定':'待检查'):'未锁定';}
      $('#server-user').textContent=$('#username').value.trim();
      const meta=$('.server-metadata');const user=$('#server-user');meta.replaceChildren(user,document.createTextNode(' / Ubuntu '+server.osVersion+' / SSH'));
      $('#mount-state').textContent=server.mountReady?'已安装':'未安装';$('#install-mount').disabled=server.mountReady;$('#install-mount').textContent=server.mountReady?'已安装':'安装';
      $('#cloud-state').textContent=server.cloudReady?'已就绪':'未安装';$('#install-cloud').disabled=server.cloudReady;$('#install-cloud').textContent=server.cloudReady?'已安装':'安装';$('#open-cloud').disabled=!server.cloudReady;
      renderTasks();
    }
    function setMetrics(stats){
      const values=$$('.network-top strong');
      values[0].textContent=stats?(stats.latencyMs===null?'未响应':Math.round(stats.latencyMs)+' ms'):'检测中';
      values[1].textContent=stats?stats.lossPercent+' %':'—';
      $('.network-top').title='每 5 秒探测一次；丢包率为最近 20 次 ICMP 探测统计。ICMP 未响应不代表 SSH 不可达。';
    }
    function $$(selector){return [...document.querySelectorAll(selector)];}
    const originalQuery=queryIpInfo;
    queryIpInfo=async function(ip){
      if(!isPublicIpv4(ip)){$('#ip-location').textContent='暂无归属地';$('#ip-score').textContent='—';$('#score-unit').textContent='';$('#rating-state').textContent='';detectedRegion=null;renderTasks();return;}
      $('#rating-state').textContent='查询中';$('#ip-location').textContent='正在查询…';$('#ip-score').textContent='—';$('#score-unit').textContent='';renderTasks();
      try{
        const info=await native('ip.info',{ip});
        $('#ip-location').textContent=info.location||'暂无归属地';$('#ip-score').textContent=info.score===null?'—':String(info.score);$('#score-unit').textContent=info.score===null?'':' / 100';$('#rating-state').textContent=info.geoSource;
        $('#score-source').href='https://ip.net.coffee/ip/'+encodeURIComponent(ip);$('#score-source').title='定位：'+info.geoSource+(info.hasGeoConflict?'（部分来源存在分歧）':'')+'；评分为第三方'+(info.isSubnetScore?'网段':'IP')+'评分。';
        const regionMap={jp:'jp',sg:'sg',gb:'gb',de:'de',kr:'kr'};detectedRegion=info.regionCode==='us'?({'New York':'us_ny','Los Angeles':'us_la'}[info.city]||null):(regionMap[info.regionCode]||null);
      }catch{detectedRegion=null;$('#rating-state').textContent='暂不可用';$('#ip-location').textContent='暂无归属地';$('#ip-score').textContent='—';}
      if(nativeServer)applyServer(nativeServer);
    };
    let connectionAttempt=0;let connectionReady=false;let connectionInFlight=false;let lastSettings=null;
    function lockWorkspace(locked){for(const id of ['deployment-panel','maintenance-panel'])$( '#'+id).inert=locked;$('#workspace-screen').classList.toggle('is-connecting',locked);$('#maintenance-mode').disabled=locked;}
    function connectionProgress(text,failed=false){$('#connection-progress').hidden=false;$('#connection-progress').classList.toggle('failed',failed);$('#connection-progress-text').textContent=text;$('#cancel-connect').hidden=failed;$('#retry-connect').hidden=!failed;$('#edit-connection').hidden=!failed;if(failed){busy=false;renderTasks();const values=$$('.network-top strong');for(const value of values)value.textContent='—';$('#rating-state').textContent='未连接';$('#ip-location').textContent='等待连接服务器';$('#server-address').textContent='待确认';}}
    function prepareWorkspace(settings){
      clearInterval(metricsTimer);nativeServer=null;connectionReady=false;connectionInFlight=true;busy=false;currentTask=-1;accountOptions={};accountDraftPending=false;detectedRegion=null;ipPolicy={lockIngress:false,lockEgress:false};
      states=Array(tasks.length).fill('pending');taskLogs=Array(tasks.length).fill('等待连接完成。');selectedTasks.clear();selecting=false;renderTasks();
      serverConnection={ingressIp:settings.host,egressIp:''};$('#server-ingress').textContent=settings.host;$('#server-address').textContent='正在检查…';$('#server-user').textContent=settings.username;
      const user=$('#server-user');$('.server-metadata').replaceChildren(user,document.createTextNode(' / 系统待检查 / SSH'));
      $('#ip-location').textContent='等待连接';$('#ip-score').textContent='—';$('#score-unit').textContent='';$('#rating-state').textContent='待检查';setMetrics(null);
      switchMode('deployment');lockWorkspace(true);showScreen('workspace');connectionProgress('正在连接服务器…');
    }
    function confirmServerIdentity(response){const changed=!!response.key.previousFingerprint;
      return appDialog(changed?'检测到服务器密钥已经变化。\n您是否重装过这台服务器？\n\n刚刚重装过，可选择“是”重新信任并连接；没有重装请选择“否”并核实服务器。':'首次连接这台服务器。\n请核对服务器身份，确认后建立连接。',{
            title:changed?'是否重装过系统？':'确认服务器身份',confirm:changed?'是，重新连接':'信任并连接',cancel:changed?'否，取消':'取消',
            details:'服务器：'+response.key.host+':'+response.key.port+(changed?'\n原指纹：'+response.key.previousFingerprint:'')+'\n新指纹：'+response.key.fingerprint});
    }
    async function connect(settings){
      const attempt=++connectionAttempt;lastSettings=settings;
      $('#password').type='password';setEye($('#reveal'),false);prepareWorkspace(settings);
      // 先呈现主界面，再开始 SSH；慢连接、身份确认和评分均留在主界面处理。
      await native('window.resize',{wide:true});await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      try{
        let response;
        for(;;){
          if(attempt!==connectionAttempt)return null;
          response=await native('connect',{host:settings.host,username:settings.username,password:settings.password,port:settings.port||sshPort});
          if(attempt!==connectionAttempt)return null;
          if(!response.requiresTrust)break;
          const changed=!!response.key.previousFingerprint;
          connectionProgress(changed?'等待确认：服务器密钥已变化。':'等待确认服务器身份。');
          const accepted=await confirmServerIdentity(response);
          if(attempt!==connectionAttempt)return null;
          if(!accepted){connectionInFlight=false;await native('connection.cancel');connectionProgress('已取消连接，原信任记录未修改。',true);return null;}
          await native('host.trust',{token:response.token});connectionProgress('正在重新连接服务器…');
        }
        const result=response.server;try{accountOptions=await native('account.draft.get')||{};accountDraftPending=Object.keys(accountOptions).length>0;}catch{accountOptions={};accountDraftPending=false;}
        $('#password').value='';serverConnection={ingressIp:result.ipPolicy?.ingressIp||settings.host,egressIp:result.ipPolicy?.egressIp||result.observedEgressIp||''};$('#server-ingress').textContent=serverConnection.ingressIp;$('#server-address').textContent=serverConnection.egressIp||'待确认';
        taskLogs=Array(tasks.length).fill('');busy=false;connectionReady=true;connectionInFlight=false;lockWorkspace(false);applyServer(result);
        $('#connection-progress').hidden=true;
        const measure=async()=>{if(metricsBusy||!connectionReady)return;metricsBusy=true;try{setMetrics(await native('metrics'));}catch{setMetrics({latencyMs:null,lossPercent:100});}finally{metricsBusy=false;}};metricsTask=measure();metricsTimer=setInterval(measure,5000);
        // 评分不会阻塞连接完成或部署操作。
        void queryIpInfo(serverConnection.egressIp).then(()=>{if(attempt===connectionAttempt&&nativeServer)applyServer(nativeServer);});
        if(result.activeJobs?.length)resumeJob(result.activeJobs[0]);return result;
      }catch(error){
        if(attempt!==connectionAttempt)return null;
        connectionReady=false;connectionInFlight=false;lockWorkspace(true);connectionProgress(error.message,true);
        await showAppMessage(error.message,{title:'连接未完成'});return null;
      }finally{resetLogin();}
    }
    $('#cancel-connect').onclick=async()=>{connectionAttempt++;connectionInFlight=false;connectionProgress('正在取消连接…');await native('connection.cancel');connectionProgress('已取消连接。',true);};
    $('#edit-connection').onclick=()=>{connectionAttempt++;native('connection.cancel');showScreen('login');resetLogin();};
    $('#retry-connect').onclick=()=>{if(lastSettings)void connect(lastSettings);};
    $('#login-form').onsubmit=async e=>{e.preventDefault();await connect({host:$('#ip').value.trim(),username:$('#username').value.trim(),password:$('#password').value,port:sshPort});};
    async function resumeJob(job){
      busy=true;const advanced=job.operation==='cloud'||job.operation==='mount_install';if(advanced)beginOperation(job.operation,job.operation==='cloud'?'恢复 CloudCLI 安装进度':'恢复目录挂载安装进度');currentTask=ids.indexOf(job.operation);
      if(currentTask>=0){states[currentTask]='running';taskLogs[currentTask]='恢复远端部署监控…';}renderTasks();
      try{applyServer(await native('job.resume',job));if(advanced){appendOperationLog('后台安装任务已完成。');advancedOperation.status='安装完成。';}}
      catch(e){if(advanced){appendOperationLog('安装未完成：'+e.message);advancedOperation.status='安装未完成，请查看日志后重试。';}else showAppMessage(e.message);}
      finally{if(advanced){advancedOperation.done=true;if(displayedOperation===advancedOperation)$('#operation-log-status').textContent=advancedOperation.status;}currentTask=-1;busy=false;renderTasks();}
    }
    async function runTask(index){
      const previousState=states[index];currentTask=index;states[index]='running';taskLogs[index]='正在连接 Ubuntu 部署模块…';renderTasks();
      try{
        const preset=currentLocale();
        const locale=preset?{region:preset.name,timezone:preset.timezone,locale:preset.locale,language:preset.language}:null;
        const result=await native('deploy',{step:ids[index],locale,username:accountOptions.username||nativeServer?.username||'workspace',hostname:accountOptions.hostname||'',password:accountOptions.password||''});applyServer(result);
        if(!result.statuses?.[ids[index]])throw new Error(tasks[index][0]+'验证未通过，请查看日志。');
        taskLogs[index]='部署完成，已通过实际状态检查。';if(index===0){accountOptions={};accountDraftPending=false;}
      }catch(error){states[index]=index===0&&previousState==='done'?'done':'pending';taskLogs[index]='失败：'+error.message;throw error;}
      finally{currentTask=-1;renderTasks();}
    }
    deployOne=async function(index){if(busy||(states[index]==='done'&&!(index===0&&accountDraftPending)))return;selecting=false;selectedTasks.clear();busy=true;try{await runTask(index);}catch(error){showAppMessage(error.message);}finally{busy=false;renderTasks();}};
    $('#deploy-all').onclick=async()=>{
      if(busy)return;if(!selecting){selecting=true;selectedTasks.clear();renderTasks();return;}
      const queue=[...selectedTasks].filter(i=>states[i]!=='done').sort((a,b)=>a-b);if(!queue.length)return;
      selecting=false;selectedTasks.clear();busy=true;
      try{for(const i of queue)await runTask(i);}catch(error){showAppMessage(error.message);}finally{busy=false;renderTasks();}
    };
    $('#account-card').onclick=async()=>{
      try{
        const account=await native('account');$('#result-body').replaceChildren();
        for(const [name,value] of [['用户名',account.username],['密码',account.password]]){
          const row=document.createElement('div');row.className='credential-row';const label=document.createElement('span');label.textContent=name;
          const group=document.createElement('div');group.className='credential-value';const code=document.createElement('code');code.textContent=value;
          const copy=document.createElement('button');copy.className='copy-button';copy.setAttribute('aria-label','复制'+name);copy.textContent='⧉';copy.onclick=async()=>{await native('copy',{text:value});copy.title='已复制';setTimeout(()=>copy.title='复制',1200);};group.append(code,copy);row.append(label,group);$('#result-body').append(row);
        }
        $('#maintenance-result').showModal();
      }catch(error){showAppMessage(error.message);}
    };
    async function runMaintenanceCard(selector,action){const card=$(selector);const caption=card.querySelector('span');const text=caption.textContent;card.disabled=true;caption.textContent='正在连接…';try{await action();}catch(e){showAppMessage(e.message);}finally{card.disabled=false;caption.textContent=text;}}
    $('#desktop-card').onclick=()=>runMaintenanceCard('#desktop-card',()=>native('desktop'));
    $('#terminal-card').onclick=()=>runMaintenanceCard('#terminal-card',async()=>{const result=await native('terminal');if(result)applyServer(result);});
    let advancedOperation=null;let displayedOperation=null;const operationHistory=new Map();
    function appendOperationLog(line){
      if(!advancedOperation)return;advancedOperation.lines.push(line);if(advancedOperation.lines.length>300)advancedOperation.lines.shift();
      const key=advancedOperation.key==='cloud'?'cloud':'mount';$('#'+key+'-live-log').hidden=false;$('#'+key+'-live-log').textContent='查看日志：'+line;
      if(displayedOperation===advancedOperation){const box=$('#operation-log-output');box.textContent=advancedOperation.lines.join('\n');box.scrollTop=box.scrollHeight;}
    }
    function beginOperation(key,label){advancedOperation={key,label,lines:[],started:Date.now(),done:false,status:'正在准备…'};operationHistory.set(key,advancedOperation);displayedOperation=advancedOperation;$('#operation-log-title').textContent=label;$('#operation-log-status').textContent='正在准备…';$('#operation-log-output').textContent='';$('#operation-log-dialog').showModal();appendOperationLog('已开始，请等待服务器返回实际进度。');}
    function syncAdvancedButtons(){
      for(const [key,ready] of [['mount',nativeServer?.mountReady],['cloud',nativeServer?.cloudReady]]){const button=$('#install-'+key);button.disabled=!!ready||busy;button.title=busy?'有任务正在执行，请查看进度。':'';button.textContent=advancedOperation&&!advancedOperation.done&&advancedOperation.key===(key==='cloud'?'cloud':'mount_install')?'安装中…':ready?'已安装':'安装';}
    }
    for(const key of ['mount','cloud']){const log=document.createElement('p');log.id=key+'-live-log';log.className='advanced-live-log';log.hidden=true;$('#'+key+'-summary')?.after(log);if(!log.isConnected)$('#install-'+key).closest('.advanced-card').querySelector('.advanced-copy>div').append(log);log.title='点击查看安装日志';log.onclick=()=>{const op=operationHistory.get(key==='cloud'?'cloud':'mount_install');if(op){displayedOperation=op;$('#operation-log-title').textContent=op.label;$('#operation-log-status').textContent=op.status;$('#operation-log-output').textContent=op.lines.join('\n');$('#operation-log-dialog').showModal();}};}
    setInterval(()=>{if(advancedOperation&&!advancedOperation.done)$('#operation-log-status').textContent='正在安装，已用时 '+Math.floor((Date.now()-advancedOperation.started)/1000)+' 秒。'},1000);
    async function installAdvanced(key){
      if(busy){if(advancedOperation&&!advancedOperation.done)$('#operation-log-dialog').showModal();else showAppMessage('已有任务正在执行，请等待完成。');return;}
      busy=true;const id=key==='cloud'?'cloud':'mount_install';beginOperation(id,key==='cloud'?'安装 CloudCLI 网页界面':'安装目录挂载组件');renderTasks();syncAdvancedButtons();
      try{applyServer(await native(key==='cloud'?'cloud.install':'mount.install'));appendOperationLog('安装完成，已通过服务器状态检查。');advancedOperation.status='安装完成。';if(displayedOperation===advancedOperation)$('#operation-log-status').textContent=advancedOperation.status;}
      catch(e){appendOperationLog('安装失败：'+e.message);advancedOperation.status='安装未完成，请查看日志后重试。';if(displayedOperation===advancedOperation)$('#operation-log-status').textContent=advancedOperation.status;$('#'+key+'-state').textContent='安装失败';}
      finally{advancedOperation.done=true;busy=false;renderTasks();syncAdvancedButtons();}
    }
    $('#install-cloud').onclick=()=>installAdvanced('cloud');$('#install-mount').onclick=()=>installAdvanced('mount');
    $('#open-cloud').onclick=()=>native('cloud.open').catch(e=>showAppMessage(e.message));
    let selectedRemoteField=null;let folderEpoch=0;let lastFolderPath='';let folderTimer;
    function appendFolderLog(line){const box=$('#folder-log');box.textContent=(box.textContent+'\n'+line).trim().split('\n').slice(-20).join('\n');box.scrollTop=box.scrollHeight;}
    async function listRemote(path=''){
      const epoch=++folderEpoch;lastFolderPath=path;clearInterval(folderTimer);$('#folder-log').textContent='';$('#folder-error').textContent='';$('#folder-list').textContent='正在读取…';$('#folder-confirm').disabled=true;$('#folder-up').disabled=true;$('#folder-retry').hidden=true;
      $('#folder-path').textContent=path||(nativeServer?.homeDirectory?nativeServer.homeDirectory+'/workspace':'工作目录');appendFolderLog('正在通过 SSH 读取工作用户目录…');const started=Date.now();
      folderTimer=setInterval(()=>appendFolderLog('仍在等待服务器响应，已用时 '+Math.floor((Date.now()-started)/1000)+' 秒。'),3000);
      try{
        const result=await native('folder.list',{path});if(epoch!==folderEpoch)return;
        lastFolderPath=result.path;$('#folder-path').textContent=result.path;$('#folder-up').disabled=result.path===result.parent;$('#folder-up').onclick=()=>listRemote(result.parent);$('#folder-list').replaceChildren();
        for(const dir of result.directories){const button=document.createElement('button');button.textContent=dir.name;button.onclick=()=>listRemote(dir.path);$('#folder-list').append(button);}
        if(!result.directories.length)$('#folder-list').textContent='此目录没有下级文件夹。';$('#folder-confirm').disabled=false;appendFolderLog('读取完成：'+result.directories.length+' 个目录。'+(result.truncated?'仅显示前 300 个，可手动填写更具体路径。':''));
      }catch(e){if(epoch!==folderEpoch)return;$('#folder-list').textContent='';$('#folder-error').textContent=e.message;appendFolderLog('读取未完成，可点击重试或返回上一级。');$('#folder-retry').hidden=false;const current=$('#folder-path').textContent;if(current.startsWith('/')&&current!=='/'){const parent=current.slice(0,current.lastIndexOf('/'))||'/';$('#folder-up').disabled=false;$('#folder-up').onclick=()=>listRemote(parent);}}
      finally{if(epoch===folderEpoch)clearInterval(folderTimer);}
    }
    $('#folder-retry').onclick=()=>listRemote(lastFolderPath);
    $('#remote-folder-dialog').addEventListener('close',()=>{folderEpoch++;clearInterval(folderTimer);native('folder.cancel').catch(()=>{});});
    $('#folder-confirm').onclick=()=>{if(selectedRemoteField)selectedRemoteField.value=$('#folder-path').textContent;$('#remote-folder-dialog').close();};
    const originalAppendMountPair=appendMountPair;appendMountPair=function(pair){originalAppendMountPair(pair);const row=$('#mount-list').lastElementChild;for(const key of ['local','remote']){const field=row.querySelector('[data-path='+key+']');if(key==='remote'&&nativeServer?.homeDirectory)field.placeholder=nativeServer.homeDirectory+'/workspace/projects';const wrap=document.createElement('span');wrap.className='folder-input';field.replaceWith(wrap);wrap.append(field);const picker=document.createElement('button');picker.type='button';picker.textContent='选择目录';picker.onclick=async()=>{if(key==='local'){try{const selected=await native('pickFolder');if(selected)field.value=selected;}catch(e){showAppMessage(e.message);}}else{selectedRemoteField=field;$('#remote-folder-dialog').showModal();await listRemote();}};wrap.append(picker);}};
    openAccountSettings=async function(){
      try{const draft=await native('account.draft.get');accountOptions=draft||{};accountDraftPending=!!draft;
        $('#custom-hostname').value=draft?.hostname||nativeServer?.hostname||'safe-workstation';$('#custom-username').value=draft?.username||nativeServer?.username||'workspace';$('#custom-password').type='password';$('#custom-password').value=draft?.password||'';setEye($('#account-settings [data-reveal]'),false);
        $('#account-custom-hint').textContent='这里只保存本机参数，点击该步骤的“开始部署”或“更新”后才应用。更换用户名会创建新环境，保留旧账户与项目，需先注销旧桌面并卸载目录，旧 CloudCLI 将停止。';$('#account-settings').showModal();
      }catch(e){showAppMessage(e.message);}
    };
    $('#account-settings-form').onsubmit=async e=>{
      e.preventDefault();if(busy)return;const draft={username:$('#custom-username').value.trim(),hostname:$('#custom-hostname').value.trim(),password:$('#custom-password').value};
      try{await native('account.draft.save',draft);accountOptions=draft;accountDraftPending=true;$('#custom-password').value='';$('#account-settings').close();taskLogs[0]='参数已保存，等待'+(states[0]==='done'?'更新':'开始部署')+'后应用。';renderTasks();}catch(e){showAppMessage(e.message);}
    };
    $('#desktop-resolution').onclick=async()=>{try{const value=await native('desktop.resolution.get');$('#resolution-select').value=value.width+'x'+value.height;$('#resolution-dialog').showModal();}catch(e){showAppMessage(e.message);}};
    $('#resolution-form').onsubmit=async e=>{e.preventDefault();const [width,height]=$('#resolution-select').value.split('x').map(Number);try{const result=await native('desktop.resolution.save',{width,height});$('#resolution-dialog').close();if(result.deferred>0)showAppMessage('窗口大小已调整。当前服务器未接受在线分辨率切换，新像素分辨率将在下次打开桌面时使用。');}catch(e){showAppMessage(e.message);}};
    native('login.load').then(saved=>{if(saved&&$('#workspace-screen').hidden){$('#ip').value=saved.host;$('#username').value=saved.username;$('#password').value=saved.password;sshPort=saved.port||22;}}).catch(()=>{});
    $('#mount-settings').onclick=async()=>{try{mountPairs=await native('mount.settings');$('#mount-list').replaceChildren();(mountPairs.length?mountPairs:[{local:'',remote:''}]).forEach(appendMountPair);$('#mount-dialog').showModal();}catch(e){showAppMessage(e.message);}};
    let mountSaving=false;
    const mountProgress=document.createElement('div');mountProgress.className='connection-progress';mountProgress.hidden=true;mountProgress.setAttribute('role','status');mountProgress.setAttribute('aria-live','polite');
    const mountSpinner=document.createElement('span');mountSpinner.className='connection-spinner';mountSpinner.setAttribute('aria-hidden','true');
    const mountProgressText=document.createElement('span');const mountElapsed=document.createElement('span');mountProgress.append(mountSpinner,mountProgressText,mountElapsed);$('#mount-form .mount-footer').before(mountProgress);
    $('#mount-dialog').addEventListener('cancel',e=>{if(mountSaving)e.preventDefault();});
    $('#mount-form').onsubmit=async e=>{
      e.preventDefault();if(mountSaving)return;if(busy){showAppMessage('已有任务正在执行，请等待完成。');return;}if(!$('#mount-form').reportValidity())return;
      const mappings=$$('#mount-list .mount-pair').map(row=>({local:row.querySelector('[data-path="local"]').value.trim(),remote:row.querySelector('[data-path="remote"]').value.trim()}));
      mountSaving=true;busy=true;const form=$('#mount-form'),button=form.querySelector('[type="submit"]');const controls=[...form.querySelectorAll('button,input'),$('#close-mount')].map(control=>[control,control.disabled]);
      for(const [control] of controls)control.disabled=true;form.setAttribute('aria-busy','true');button.textContent='正在挂载…';mountProgress.hidden=false;mountProgressText.textContent=mappings.length?'正在建立文件通道并检查目录…':'正在同步并卸载目录…';mountElapsed.textContent='已用时 0 秒';
      const started=Date.now();const timer=setInterval(()=>{mountElapsed.textContent='已用时 '+Math.floor((Date.now()-started)/1000)+' 秒';},1000);renderTasks();
      try{await native('mount.apply',{mappings});mountPairs=mappings;$('#mount-summary').textContent=mappings.length?'已挂载 '+mappings.length+' 组目录。':'已卸载所有目录。';$('#mount-dialog').close();}catch(error){showAppMessage(error.message);}
      finally{clearInterval(timer);mountSaving=false;busy=false;mountProgress.hidden=true;form.removeAttribute('aria-busy');for(const [control,disabled] of controls)control.disabled=disabled;button.textContent='保存设置';renderTasks();}
    };
    $('#ip-policy-form').onsubmit=async e=>{
      e.preventDefault();const args={ingressIp:$('#ingress-ip-input').value.trim(),egressIp:$('#egress-ip-input').value.trim(),lockIngress:$('#lock-ingress').checked,lockEgress:$('#lock-egress').checked};
      try{const result=await native('ip.apply',args);serverConnection={ingressIp:args.ingressIp,egressIp:args.egressIp};$('#server-ingress').textContent=args.ingressIp;$('#server-address').textContent=args.egressIp;ipPolicy=args;$('#ip-policy-state').textContent='已应用';$('#ip-policy-dialog').close();if(result)applyServer(result);await queryIpInfo(args.egressIp);}catch(error){showAppMessage(error.message);}
    };
    // 验收入口仅在桌面启动参数指定了私密配置文件时由宿主调用。
    window.safeCodeRunAcceptance=async config=>{
      try{
        await native('acceptance.capture',{tag:'notice'});if($('#accept').getBoundingClientRect().bottom>innerHeight||$('#exit').getBoundingClientRect().bottom>innerHeight)throw new Error('声明按钮超出窗口。');$('#username').value=config.username;$('#ip').value=config.host;originalShowScreen('login');await native('window.resize',{wide:false});await native('acceptance.capture',{tag:'login'});const connectBounds=$('#connect').getBoundingClientRect();const lowest=Math.max(connectBounds.bottom,advanced.getBoundingClientRect().bottom,$('#back').getBoundingClientRect().bottom);const loginLayout={buttonBottom:connectBounds.bottom,controlsBottom:lowest,viewportHeight:innerHeight,visible:lowest<=innerHeight};if(!loginLayout.visible)throw new Error('登录连接按钮超出窗口。');const restored=await native('login.load');const restoredLogin=restored?.host===config.host&&restored?.username===config.username&&restored?.password===config.password;let result=await connect(config);config.password='';
        result=await native('deploy',{step:'ssh',username:'workspace'});applyServer(result);await metricsTask;
        $('#reveal').click();if($('#password').type!=='text'||!$('#reveal svg')||$('#reveal').textContent)throw new Error('密码显示按钮异常');$('#reveal').click();
        const realIp=$('#server-ingress').textContent;$('#server-ingress').nextElementSibling.click();if($('#server-ingress').textContent===realIp)throw new Error('IP 隐藏失败');$('#server-ingress').nextElementSibling.click();if($('#server-ingress').textContent!==realIp)throw new Error('IP 恢复失败');
        const dirs=await native('folder.list');if(!dirs.path.endsWith('/workspace'))throw new Error('远程目录起始位置错误');await native('folder.list',{path:dirs.parent});
        $('#mount-list').replaceChildren();appendMountPair({local:'',remote:''});if(document.querySelectorAll('.folder-input button').length!==2)throw new Error('目录选择按钮位置错误');
        await native('acceptance.capture',{tag:'deployment'});await openAccountSettings();await native('acceptance.capture',{tag:'custom'});$('#account-settings').close();await $('#mount-settings').onclick();await native('acceptance.capture',{tag:'mount'});$('#mount-dialog').close();const clientChecks=await native('client.verify');switchMode('maintenance');
        clientChecks.restoredLogin=restoredLogin;clientChecks.passwordEye=true;clientChecks.ipMask=true;clientChecks.remoteDirectory=true;clientChecks.embeddedPickers=true;

        await native('acceptance.done',{success:true,stepCount:ids.length,osId:result.osId,statuses:result.statuses,clientChecks,loginLayout,location:$('#ip-location').textContent,geoSource:$('#rating-state').textContent,windowWidth:innerWidth,windowHeight:innerHeight});
      }catch(error){await native('acceptance.done',{success:false,error:error.message});}
    };
    let servicePreferences=null;let update=null;
    function showPreferences(p){servicePreferences=p;$('#usage-consent').checked=p.enabled;$('#settings-usage-consent').checked=p.enabled;$('#usage-recipient').textContent=p.serviceAvailable?'接收方：'+p.recipient+'。统计库不保存来源 IP；网络连接会向接收服务器展示来源 IP。':'反馈与统计服务尚未启用，目前不会上传。';$('#feedback-send').disabled=!p.serviceAvailable;}
    $('#accept').disabled=true;for(const id of ['usage-consent','settings-usage-consent'])document.getElementById(id).disabled=true;
    const preferencesReady=native('services.preferences').then(showPreferences).catch(()=>{$('#usage-consent').checked=false;$('#settings-usage-consent').checked=false;}).finally(()=>{$('#accept').disabled=false;for(const id of ['usage-consent','settings-usage-consent'])document.getElementById(id).disabled=false;});
    for(const id of ['usage-consent','settings-usage-consent'])document.getElementById(id).onchange=e=>{const enabled=e.target.checked;$('#usage-consent').checked=enabled;$('#settings-usage-consent').checked=enabled;native('services.consent',{enabled,confirmed:false}).then(showPreferences).catch(e=>showAppMessage(e.message));};
    const previousAccept=$('#accept').onclick;$('#accept').onclick=async()=>{try{await native('notice.accept');previousAccept();native('services.consent',{enabled:$('#usage-consent').checked,confirmed:true}).then(showPreferences).catch(()=>{});}catch(e){showAppMessage('无法保存声明确认，请检查本机存储空间。');}};
    const startupReady=preferencesReady.then(()=>native('notice.state')).then(state=>{if(state.accepted&&(!window.__safeCodeAcceptance||window.__safeCodeConnectionAcceptance)){showScreen('login');native('services.consent',{enabled:$('#usage-consent').checked,confirmed:true}).then(showPreferences).catch(()=>{});}else{showScreen('notice');}}).catch(()=>{showScreen('notice');});
    async function checkUpdate(force=false){$('#check-update').disabled=true;$('#update-status').textContent='正在检查 GitHub…';try{update=await native('updates.check',{force});$('#update-status').textContent='当前版本 '+update.currentVersion+' · '+update.status;$('#release-page').hidden=!update.page;$('#download-update').hidden=!update.canInstall;if(update.version)$('#services-entry').textContent='发现新版本';}catch(e){$('#update-status').textContent='检查失败：'+e.message;}finally{$('#check-update').disabled=false;}}
    $('#check-update').onclick=()=>checkUpdate(true);
    $('#release-page').onclick=()=>{if(update?.page)window.open(update.page,'_blank');};
    window.safeCodeUpdateProgress=(done,total)=>{$('#update-status').textContent='正在下载 '+Math.round(done/total*100)+'%';};
    $('#download-update').onclick=async()=>{$('#download-update').disabled=true;try{await native('updates.download');$('#update-status').textContent='下载完成，SHA256 校验通过。安装时会关闭当前客户端。';$('#download-update').hidden=true;$('#install-update').hidden=false;}catch(e){$('#update-status').textContent=e.message;}finally{$('#download-update').disabled=false;}};
    $('#install-update').onclick=()=>{if(busy){$('#update-status').textContent='请等待当前操作完成后安装更新。';return;}native('updates.install').catch(e=>{$('#update-status').textContent=e.message;});};
    $('#feedback-form').onsubmit=async e=>{e.preventDefault();$('#feedback-send').disabled=true;try{await native('feedback.send',{message:$('#feedback-message').value});$('#feedback-message').value='';$('#feedback-status').textContent='已提交，谢谢你的反馈。';}catch(e){$('#feedback-status').textContent=e.message;}finally{$('#feedback-send').disabled=!servicePreferences?.serviceAvailable;}};
    // 版本查询与统计授权相互独立，验收模式不调用外部服务。
    if(!window.__safeCodeAcceptance)setTimeout(()=>checkUpdate(false),1500);
    window.safeCodeRunServicesAcceptance=async()=>{
      try{
        const windowSizing=await native('acceptance.window-sizes');const prefs=await native('services.preferences');showPreferences(prefs);if(!prefs.enabled)throw new Error('首次统计默认值错误');
        await native('acceptance.capture',{tag:'notice'});if($('#exit').getBoundingClientRect().bottom>innerHeight||$('#usage-consent').getBoundingClientRect().bottom>$('#accept').getBoundingClientRect().top)throw new Error('声明页布局溢出');
        await native('services.consent',{enabled:false,confirmed:false});const disabled=await native('services.preferences');if(disabled.enabled)throw new Error('关闭统计失败');showPreferences(disabled);
        $('#services-dialog').showModal();if($('#feedback-send').getBoundingClientRect().bottom>$('#services-dialog').getBoundingClientRect().bottom)throw new Error('反馈按钮超出窗口');await checkUpdate(true);await native('acceptance.capture',{tag:'services'});const feedbackMarker='SafeCode Windows integration test '+crypto.randomUUID();await native('feedback.send',{message:feedbackMarker});$('#services-dialog').close();
        await native('services.consent',{enabled:true,confirmed:false});
        await native('acceptance.done',{success:true,windowSizing,defaultEnabled:prefs.enabled,optOutSaved:!disabled.enabled,serviceAvailable:prefs.serviceAvailable,recipient:prefs.recipient,feedbackMarker,feedbackSentWithStatsDisabled:true,updateStatus:$('#update-status').textContent,viewportHeight:innerHeight});
      }catch(error){await native('acceptance.done',{success:false,error:error.message});}
    };
    window.safeCodeRunConnectionAcceptance=async config=>{
      const waitFor=async predicate=>{const end=Date.now()+15000;while(!predicate()){if(Date.now()>end)throw new Error('等待界面超时');await new Promise(r=>setTimeout(r,30));}};
      const centered=dialog=>{const b=dialog.getBoundingClientRect();if(Math.abs(b.left+b.width/2-innerWidth/2)>2||Math.abs(b.top+b.height/2-innerHeight/2)>2||b.right>innerWidth||b.bottom>innerHeight)throw new Error('弹窗没有居中或超出窗口');};
      try{
        await startupReady;
        if(config.phase==='restart'){
          if($('#login-screen').hidden||!$('#notice-screen').hidden)throw new Error('再次启动仍显示声明');
          await native('acceptance.capture',{tag:'login'});await native('acceptance.done',{success:true,noticeSkippedAfterRestart:true});return;
        }
        $('#usage-consent').checked=false;await $('#accept').onclick();if(!(await native('notice.state')).accepted)throw new Error('声明确认未保存');
        advanced.click();centered($('#advanced-dialog'));await native('acceptance.capture',{tag:'advanced'});$('#advanced-dialog .close').click();if($('#advanced-dialog').open)throw new Error('高级连接无法关闭');
        advanced.click();$('#ssh-port').value='2222';$('#advanced-form').requestSubmit();if(sshPort!==2222||$('#advanced-dialog').open)throw new Error('SSH 端口未保存');
        const settings={host:'127.0.0.1',username:'fixture',password:'fixture-only',port:config.silentPort};
        const pendingConnect=connect(settings);if($('#workspace-screen').hidden||!$('#deployment-panel').inert)throw new Error('连接时未立即显示主界面或操作未锁定');
        await native('acceptance.capture',{tag:'loading'});await $('#cancel-connect').onclick();await pendingConnect;if(!$('#connection-progress').classList.contains('failed'))throw new Error('取消未生效');
        const failure=connect({...settings,port:1});await waitFor(()=>$('#app-message-dialog').open);centered($('#app-message-dialog'));
        if(!$('#app-message-body').textContent.includes('服务器连接不上')||$('#app-message-body').textContent.includes('timed out'))throw new Error('连接失败未转换为中文提示');
        await native('acceptance.capture',{tag:'error'});$('#app-message-dialog [data-dialog-dismiss]').click();await failure;if($('#app-message-dialog').open)throw new Error('报错弹窗无法关闭');
        const change={key:{host:'203.0.113.10',port:22,previousFingerprint:'SHA256:fixture-old',fingerprint:'SHA256:fixture-new'}};
        const no=confirmServerIdentity(change);await waitFor(()=>$('#app-message-dialog').open);centered($('#app-message-dialog'));await native('acceptance.capture',{tag:'trust'});$('#app-message-cancel').click();if(await no)throw new Error('否被当成同意');
        const yes=confirmServerIdentity(change);await waitFor(()=>$('#app-message-dialog').open);$('#app-message-confirm').click();if(!await yes)throw new Error('是未返回确认');
        await native('acceptance.done',{success:true,immediateWorkspace:true,connectingOperationsLocked:true,cancelResponsive:true,centeredErrors:true,friendlyNetworkError:true,advancedPortSaved:true,dialogsClosable:true,reinstallYesNo:true,noticeSaved:true});
      }catch(e){await native('acceptance.done',{success:false,error:e.message});}
    };
    window.safeCodeRunMaintenanceAcceptance=async()=>{
      try{
        await startupReady;const result=await native('acceptance.connect-current');$('#username').value=result.username;nativeServer=result.server;connectionReady=true;busy=false;showScreen('workspace');lockWorkspace(false);applyServer(result.server);
        const before=await native('acceptance.hostname');await openAccountSettings();$('#custom-hostname').value='safe-draft-verification';$('#custom-username').value=result.server.username;$('#custom-password').value='fixture-password-1234';await $('#account-settings-form').onsubmit({preventDefault(){}});
        if(!accountDraftPending||states[0]!=='done'||$('#steps .step:first-child .deploy-button').textContent!=='更新'||$('#steps .step:first-child .deploy-button').disabled)throw new Error('已完成步骤未提供更新');
        if(await native('acceptance.hostname')!==before)throw new Error('保存参数修改了服务器');
        await openAccountSettings();if($('#custom-hostname').value!=='safe-draft-verification'||$('#account-settings-form button[type=submit]').textContent!=='保存')throw new Error('参数保存或按钮文案错误');await native('acceptance.capture',{tag:'draft'});$('#account-settings').close();await native('account.draft.save',{username:result.server.username,hostname:before,password:''});accountOptions={username:result.server.username,hostname:before,password:''};accountDraftPending=true;await deployOne(0);if(accountDraftPending||states[0]!=='done'||await native('acceptance.hostname')!==before)throw new Error('账户更新应用未通过或更改了测试服务器名称');
        switchMode('maintenance');await native('acceptance.capture',{tag:'maintenance'});$('#desktop-resolution').click();await new Promise(r=>setTimeout(r,300));$('#resolution-select').value='1920x1080';await $('#resolution-form').onsubmit({preventDefault(){}});const setting=await native('desktop.resolution.get');if(setting.width!==1920||setting.height!==1080)throw new Error('分辨率未保存');await $('#desktop-resolution').onclick();await native('acceptance.capture',{tag:'resolution'});$('#resolution-dialog').close();await native('desktop.resolution.save',{width:1280,height:800});
        await installAdvanced('mount');if(!nativeServer.mountReady)throw new Error('目录挂载组件未安装成功');if(!advancedOperation.lines.length)throw new Error('挂载安装无日志');await native('acceptance.capture',{tag:'install'});$('#operation-log-dialog').close();
        let cloud=null;if(!nativeServer.cloudReady){cloud=installAdvanced('cloud');await new Promise(r=>setTimeout(r,500));$('#operation-log-dialog').close();}
        await $('#mount-settings').onclick();$('#remote-folder-dialog').showModal();const started=Date.now();await listRemote();if($('#folder-confirm').disabled)throw new Error('目录读取失败：'+$('#folder-error').textContent);const folderDuration=Date.now()-started;if(folderDuration>12000)throw new Error('目录读取未受超时约束');await native('acceptance.capture',{tag:'folder'});$('#remote-folder-dialog').close();$('#mount-dialog').close();
        if(cloud)await cloud;if(!nativeServer.cloudReady)throw new Error('CloudCLI 安装未完成');const page=await native('acceptance.cloud-http');if(page.status!==200)throw new Error('CloudCLI 网页无法打开');$('#operation-log-dialog').close();
        await native('acceptance.done',{success:true,draftSaveDidNotApply:true,completedAccountUpdateEnabled:true,idempotentAccountUpdateApplied:true,resolutionPersisted:true,mountInstalled:nativeServer.mountReady,cloudInstalled:nativeServer.cloudReady,cloudHttp:page.status,folderReadMilliseconds:folderDuration,folderReadSucceeded:true,hostnameBefore:before});
      }catch(e){await native('acceptance.done',{success:false,error:e.message});}
    };
    window.safeCodeNativeReady=true;
  })();
}
