/* Windows 客户端桥接。普通浏览器继续使用概念稿；桌面模式不执行模拟部署。 */
if (window.chrome?.webview && (location.host === 'safe-code.local' || (window.__safeCodePlatform === 'macos' && location.protocol === 'file:'))) {
  (() => {
    const isMac=window.__safeCodePlatform==='macos';
    const servicesWindow=isMac&&window.__safeCodeServicesWindow;
    const pending = new Map();
    const native = (op,args={},deadline) => new Promise((resolve,reject) => {
      if(developerPreview&&!/^(window\.|services\.|diagnostics\.|notice\.|updates\.|feedback\.|copy$|mount\.status\.open$|preview\.|acceptance\.|claudeDesktop\.open$|desktopKey\.|codex\.open$)/.test(op))return reject(new Error('开发预览未连接服务器，此操作仅在真实连接后可用。'));
      const id=crypto.randomUUID();
      const timer=deadline&&setTimeout(()=>{
        if(pending.delete(id))reject(new Error(deadline.message));
      },deadline.ms);
      pending.set(id,{resolve,reject,timer});
      try{window.chrome.webview.postMessage({id,op,args});}
      catch(error){pending.delete(id);clearTimeout(timer);reject(error);}
    });
    window.safeCodeNativeResponse = ({id,result,error}) => {
      const item=pending.get(id);if(!item)return;pending.delete(id);clearTimeout(item.timer);
      error?item.reject(new Error(error)):item.resolve(result);
    };
    let developerPreview=false;
    let cloudLoginAttempt=null;
    const consentIds=['usage-consent','settings-usage-consent','diagnostics-consent','settings-diagnostics-consent'];
    let servicePreferences=null, update=null;
    function showPreferences(p){
      servicePreferences=p;
      for(const id of consentIds)document.getElementById(id).checked=id.includes('diagnostics')?!!p.diagnosticsEnabled:!!p.enabled;
      $('#usage-recipient').textContent=p.serviceAvailable?'接收方：'+p.recipient+'。服务数据库不保存来源 IP；网络连接会向接收服务器展示来源 IP。':'反馈与统计服务尚未启用，目前不会上传。';
      $('#feedback-send').disabled=!p.serviceAvailable;
      $('#feedback-status').textContent=p.issueId?'本次反馈将关联最近失败的问题编号：'+p.issueId:'提交你填写的文字及软件版本，当前没有可关联的失败诊断。';
    }
    async function refreshPreferences(){
      try{showPreferences(await native('services.preferences'));}
      catch{
        if(!servicePreferences)for(const id of consentIds)document.getElementById(id).checked=false;
        $('#feedback-send').disabled=true;
      }
    }
    if(isMac){
      document.documentElement.classList.add('macos-client');
      $('.window-controls').hidden=true;
      for(const element of document.querySelectorAll('[data-os-label]'))element.textContent='macOS';
      window.safeCodeApplyPreferences=showPreferences;
      window.safeCodeRefreshPreferences=refreshPreferences;
    }
    for(const id of consentIds)document.getElementById(id).disabled=true;
    $('#feedback-send').disabled=true;
    const preferencesReady=refreshPreferences().finally(()=>{
      for(const id of consentIds)document.getElementById(id).disabled=false;
    });
    let consentChanges=Promise.resolve();
    for(const id of consentIds)document.getElementById(id).onchange=e=>{
      const enabled=e.target.checked, diagnostics=id.includes('diagnostics');
      consentChanges=consentChanges.then(async()=>{
        try{showPreferences(await native(diagnostics?'diagnostics.consent':'services.consent',{enabled,confirmed:false}));}
        catch(error){await refreshPreferences();showAppMessage(error.message);}
      });
    };
    function displayUpdate(info){
      update=info;
      $('#update-status').textContent='当前版本 '+info.currentVersion+' · '+info.status;
      $('#release-page').hidden=!info.page;$('#download-update').hidden=!info.canInstall;
      showUpdateNotice(info);
    }
    function showUpdateNotice(info){
      const available=!!info.version;
      $('#services-entry').textContent=available?'发现新版本':'更新与反馈';$('#services-entry').classList.toggle('has-update',available);
      const notice=$('#title-update-notice');notice.hidden=!available;notice.title=available?'发现新版本 '+info.version+'，点击查看更新':'打开更新与反馈';notice.setAttribute('aria-label',notice.title);
    }
    async function checkUpdate(force=false){$('#check-update').disabled=true;$('#update-status').textContent='正在检查 GitHub…';try{displayUpdate(await native('updates.check',{force}));}catch(e){$('#update-status').textContent='检查失败：'+e.message;}finally{$('#check-update').disabled=false;}}
    $('#check-update').onclick=()=>checkUpdate(true);
    $('#release-page').onclick=()=>{if(update?.page)window.open(update.page,'_blank');};
    window.safeCodeUpdateProgress=(done,total)=>{$('#update-status').textContent='正在下载 '+Math.round(done/total*100)+'%';};
    $('#download-update').onclick=async()=>{$('#download-update').disabled=true;try{await native('updates.download');$('#update-status').textContent='下载完成，SHA256 校验通过。'+(isMac?'请先安全卸载目录，再打开镜像替换应用。':'安装时会关闭当前客户端。');$('#download-update').hidden=true;$('#install-update').hidden=false;}catch(e){$('#update-status').textContent=e.message;}finally{$('#download-update').disabled=false;}};
    $('#install-update').onclick=()=>{if(busy){$('#update-status').textContent='请等待当前操作完成后安装更新。';return;}native('updates.install').catch(e=>{$('#update-status').textContent=e.message;});};
    $('#feedback-form').onsubmit=async e=>{e.preventDefault();$('#feedback-send').disabled=true;try{await native('feedback.send',{message:$('#feedback-message').value});$('#feedback-message').value='';$('#feedback-status').textContent='已提交，谢谢你的反馈。';}catch(e){$('#feedback-status').textContent=e.message;}finally{$('#feedback-send').disabled=!servicePreferences?.serviceAvailable;}};

    if(servicesWindow){
      document.documentElement.classList.add('macos-services-window');
      $('#services-dialog').setAttribute('open','');
      checkUpdate(false);
      window.safeCodeNativeReady=true;
      return;
    }

    if(isMac&&window.__safeCodeNativeDialogs){
      // Keep the existing DOM and handlers when AppKit moves this view to a dialog window.
      const dialogs=[...document.querySelectorAll('dialog')];
      let stack=[];
      document.documentElement.classList.add('macos-native-dialogs');
      const showModal=HTMLDialogElement.prototype.showModal;
      HTMLDialogElement.prototype.showModal=function(){
        showModal.call(this);
        if(!stack.includes(this))stack.push(this);
      };
      const sizes={
        'mount-dialog':[540,510],'client-window':[745,490],
        'login-help-dialog':[620,495],'ip-policy-dialog':[490,495],'socks-dialog':[490,505],
        'remote-folder-dialog':[460,435],'operation-log-dialog':[540,425],
        'claude-desktop-dialog':[450,320],'gateway-repair-dialog':[450,320],'cloud-remote-dialog':[470,445],
        'advanced-dialog':[355,235],'resolution-dialog':[355,305],
        'create-folder-dialog':[380,250],'desktop-key-dialog':[360,190],'key-list-dialog':[480,265],
        'app-message-dialog':[400,300]
      };
      const observer=new MutationObserver(()=>{
        stack=stack.filter(dialog=>dialog.open);
        const panels=stack.map(dialog=>{
          const heading=dialog.querySelector(':scope>.titlebar .app-name');
          const [width,height]=sizes[dialog.id]||[420,390];
          return {id:dialog.id,title:heading?.textContent.trim()||'Safe Code',width,height};
        });
        native('window.dialogs',{panels}).catch(()=>{});
      });
      for(const dialog of dialogs){
        observer.observe(dialog,{attributes:true,attributeFilter:['open']});
        const title=dialog.querySelector(':scope>.titlebar .app-name');
        if(title)observer.observe(title,{childList:true,characterData:true,subtree:true});
      }
      const fittedDialogs=dialogs.filter(dialog=>dialog.id!=='client-window');
      const dialogContents=dialog=>[...dialog.children].filter(child=>!child.classList.contains('titlebar'));
      const fitDialog=dialog=>{
        if(!dialog?.classList.contains('native-dialog-front'))return;
        const height=Math.ceil(dialogContents(dialog).reduce((sum,child)=>{
          if(!child.getClientRects().length)return sum;
          const style=getComputedStyle(child);
          return sum+child.getBoundingClientRect().height+parseFloat(style.marginTop)+parseFloat(style.marginBottom);
        },0));
        if(height>0)native('window.fitDialog',{id:dialog.id,height}).catch(()=>{});
      };
      const contentObserver=new ResizeObserver(entries=>{
        const front=entries.map(({target})=>target.closest('dialog.native-dialog-front')).find(Boolean);
        if(front)fitDialog(front);
      });
      fittedDialogs.flatMap(dialogContents).forEach(content=>contentObserver.observe(content));
      window.safeCodePresentNativeDialog=id=>{
        for(const dialog of dialogs)dialog.classList.toggle('native-dialog-front',dialog.id===id);
        document.documentElement.classList.toggle('native-dialog-window',!!id);
        fitDialog(fittedDialogs.find(dialog=>dialog.id===id));
      };
      window.safeCodeCloseNativeDialog=id=>{
        const dialog=stack.at(-1);
        if(dialog?.id!==id||!dialog.open)return;
        if(typeof dialog.requestClose==='function')dialog.requestClose();
        else if(dialog.dispatchEvent(new Event('cancel',{cancelable:true})))dialog.close();
      };
    }
    if(isMac)window.safeCodeShowUpdateNotice=showUpdateNotice;
    let hostBusy=false,hostStatus=null,hostStatusTask,hostChange=0;
    let bridgeBusy=false,bridgeStatus=null,bridgePolling=false;
    let publicGatewayOperation='',publicGatewayStatusError='';
    let publicGatewayError;
    let publicGatewayCreatedAccount;
    let publicGatewayEnabled=false,publicGatewayLoading=false,publicGatewayReachable=false,publicGatewayMissing=false;
    let publicGatewayUpgrade=false,publicGatewayNeedsCloudKey=false;
    const minimumGatewayRevision=23;
    let bundledGatewayRevision=minimumGatewayRevision, publicGatewayLegacy=false;
    let publicGatewayTask,publicGatewayTimer;
    let publicGatewaySubscription;
    let startupRetryTimer;
    let keyState={configured:false,running:false}, keyBusy=false;
    let currentTask = -1;
    let nativeServer = null;
    let sshPort = 22;
    let metricsTask = Promise.resolve();
    let metricsTimer=null;
    let resourceTimer=null;
    let environmentTimer=null;
    let accountOptions={};
    const originalRenderTasks=renderTasks;renderTasks=function(){originalRenderTasks();syncAdvancedButtons();};
    const ids = ['account','localization','desktop','chrome','input','claude','ssh'];
    window.safeCodeNativeLog = (step,line) => {
      if(step==='gateway_repair'&&repairing&&repairDialog.open){repairStatus.textContent=line;}
      else if(step==='codex_bridge'&&bridgeBusy){$('#bridge-progress').textContent=line;}
      else if(step==='mount_apply'&&mountSaving){mountProgressText.textContent=line;}
      else if(step==='folder'&&$('#remote-folder-dialog').open){appendFolderLog(line);}
      else if(step==='cloud_public'||(cloudRemoteRunning&&$('#cloud-remote-dialog').open)){appendCloudRemoteLog(line);}
      else if(currentTask>=0){taskLogs[currentTask]=line;renderTasks();}
      else if(connectionInFlight&&!$('#workspace-screen').hidden){connectionProgress(line);}
      else if($('#login-screen').hidden===false){$('#connection-message').textContent=line;}
      else if(advancedOperation&&!advancedOperation.done&&step===advancedOperation.key){appendOperationLog(line);}
    };
    window.safeCodeNativeLogs=lines=>{for(const entry of lines)window.safeCodeNativeLog(entry.step,entry.line);};
    document.documentElement.classList.add('native-client');
    const nativeStyle=document.createElement('style');nativeStyle.textContent=`html,body{overflow:hidden!important;height:100%!important;margin:0!important;padding:0!important;scrollbar-width:none!important;-ms-overflow-style:none!important}*::-webkit-scrollbar,html::-webkit-scrollbar,body::-webkit-scrollbar{display:none!important;width:0!important;height:0!important}*{scrollbar-width:none!important;-ms-overflow-style:none!important}body{padding:0;display:block;background:white;min-height:100vh}.window,.window.wide,.window.maximized{width:100%;height:100vh;max-width:none;border:0;border-radius:0;box-shadow:none;transition:none;overflow:hidden!important}html:not(.macos-client) .window::after{content:"";position:fixed;inset:0;border:1px solid #b7c3bb;pointer-events:none;z-index:1000}.screen{height:calc(100% - 43px);overflow:hidden!important}.header-controls{width:112px}.maintenance-panel .advanced-actions>span{overflow:hidden;white-space:nowrap;text-overflow:ellipsis;max-width:100%}.connection-message{overflow:hidden;white-space:nowrap;text-overflow:ellipsis}.native-log-note{white-space:normal;line-height:1.6;color:#68786d;font-size:var(--type-small)}`;document.head.append(nativeStyle);
    $('#minimize').onclick=()=>native('window.minimize');
    $('#maximize').onclick=()=>native('window.maximize');
    $('#close').onclick=()=>native('window.close');$('#exit').onclick=()=>native(isMac?'window.quit':'window.close');
    $('.titlebar').addEventListener('mousedown',e=>{if(e.button===0&&!e.target.closest('button'))native('window.drag');});
    $('.titlebar').addEventListener('dblclick',e=>{if(!e.target.closest('button'))native('window.maximize');});
    $('#client-window').addEventListener('close',()=>$('#client-frame').removeAttribute('src'));
    const originalShowScreen=showScreen;showScreen=function(name){originalShowScreen(name);native('window.resize',{wide:name==='workspace',layout:name});};
    const advanced=$('#advanced-connect');
    advanced.onclick=()=>{$('#ssh-port').value=String(sshPort);$('#advanced-dialog').showModal();$('#ssh-port').focus();};
    $('#ssh-port').addEventListener('input',()=>$('#ssh-port').setCustomValidity(''));
    $('#advanced-form').onsubmit=e=>{e.preventDefault();const input=$('#ssh-port');const port=Number(input.value);input.setCustomValidity(Number.isInteger(port)&&port>=1&&port<=65535?'':'SSH 端口必须在 1 到 65535 之间。');if(!$('#advanced-form').reportValidity())return;sshPort=port;$('#advanced-dialog').close();};
    async function beginPreview(){
      await native('preview.begin');
      developerPreview=true;connectionAttempt++;stopConnectionMonitors();connectionReady=false;connectionInFlight=false;nativeServer=null;
      resetHostControl();
      $('#advanced-dialog').close();enterDeveloperPreview();lockWorkspace(false);$('#connection-progress').hidden=true;
      setMetrics(null);for(const value of $$('.network-top strong'))value.textContent='—';
      setResources(null,'预览模式未连接服务器，不显示模拟的监控数值。');
      setEnvironmentAssessment(null,'未连接');
    }
    $('#enter-preview').onclick=async()=>{try{await beginPreview();}catch(error){await showAppMessage(error.message);}};
    $('#login-help').onclick=async()=>{if(!window.__safeCodeNativeDialogs)await native('window.resize',{wide:true});$('#login-help-dialog').showModal();};
    $('#login-help-dialog').addEventListener('close',()=>{if(!window.__safeCodeNativeDialogs&&!$('#login-screen').hidden)native('window.resize',{wide:false});});
    function applyServer(server){
      const hadAccount=!!nativeServer?.statuses?.account;
      inputManaged=!!server.inputManaged;rebootRequired=!!server.rebootRequired;
      nativeServer=server;$('#update-login-password').hidden=false;$('#disconnect-server').hidden=false;$('#host-manage').disabled=!server.statuses?.account;states=ids.map(id=>server.statuses?.[id]?'done':'pending');
      if(currentTask>=0&&states[currentTask]!=='done')states[currentTask]='running';
      const desired=currentLocale();if(desired&&(server.timezone!==desired.timezone||server.systemLocale!==desired.locale))states[1]='pending';
      if(server.ipPolicy){ipPolicy=server.ipPolicy;$('#ip-policy-state').textContent=server.ipPolicyHealthy?(ipPolicy.lockIngress&&ipPolicy.lockEgress?'已锁定':ipPolicy.lockIngress||ipPolicy.lockEgress?'部分锁定':'未锁定'):'待检查';}
      else{ipPolicy={lockIngress:false,lockEgress:false};$('#ip-policy-state').textContent='未配置';}
      const assigned=(server.assignedIps||[]).filter(isPublicIpv4);setDetectedIpOptions(assigned);
      if(server.observedEgressIp){serverConnection.egressIp=server.observedEgressIp;$('#server-address').textContent=server.observedEgressIp;}
      if(server.ipPolicy)serverConnection.ingressIp=server.ipPolicy.ingressIp;
      $('#ip-policy-summary').textContent=server.ipPolicy?.lockEgress?'住宅出口已固定；外部检测：'+(server.observedEgressIp||'待确认'):'未固定出口，当前实际出口：'+(server.observedEgressIp||'待确认');
      updateSocksCard(server.socksEgress);
      $('#mount-state').textContent=server.mountReady?'已安装':'未安装';$('#install-mount').disabled=server.mountReady;$('#install-mount').textContent=server.mountReady?'已安装':'安装';
      $('#cloud-state').textContent=server.cloudReady?'已就绪':'未安装';$('#install-cloud').disabled=server.cloudReady;$('#install-cloud').textContent=server.cloudReady?'已安装':'安装';$('#open-cloud').disabled=!server.cloudReady;if($('#open-cloud-remote'))$('#open-cloud-remote').disabled=!server.cloudReady;
      renderTasks();
      if(connectionReady&&!hadAccount&&server.statuses?.account)void refreshPublicGateway();
    }
    function setMetrics(stats){
      const values=$$('.network-top strong');
      values[0].textContent=stats?(stats.latencyMs===null?'未响应':Math.round(stats.latencyMs)+' ms'):'检测中';
      values[1].textContent=stats?stats.lossPercent+' %':'—';
      $('.network-top').title='每 5 秒探测一次；丢包率为最近 20 次 ICMP 探测统计。ICMP 未响应不代表 SSH 不可达。';
    }
    function setResources(stats,message){
      const gib=bytes=>(bytes/1024**3).toFixed(1);
      $('#resource-cpu').textContent=stats?stats.cpuPercent.toFixed(1)+' %':'—';
      $('#resource-memory').textContent=stats?gib(stats.memoryUsedBytes)+' / '+gib(stats.memoryTotalBytes)+' G':'— / — G';
      $('#resource-disk').textContent=stats?gib(stats.diskUsedBytes)+' / '+gib(stats.diskTotalBytes)+' G':'— / — G';
      $('#server-resources').title=message||'服务器实时监控，每5秒更新；内存按已用量显示（扣除可回收缓存），硬盘为根分区；容量按 GiB 显示。';
    }
    function startResourceMonitor(attempt){
      let resourceBusy=false;
      const refresh=async()=>{
        if(resourceBusy||!connectionReady||developerPreview||attempt!==connectionAttempt||document.hidden||$('#workspace-screen').hidden)return;
        resourceBusy=true;
        try{const stats=await native('resources');if(attempt===connectionAttempt&&connectionReady&&!developerPreview)setResources(stats);}
        catch{if(attempt===connectionAttempt&&connectionReady&&!developerPreview)setResources(null,'资源监控暂不可用，下次自动重试。');}
        finally{resourceBusy=false;}
      };
      clearInterval(resourceTimer);void refresh();resourceTimer=setInterval(refresh,5000);
    }
    function setEnvironmentAssessment(result,message){
      const output=$('#environment-rating-result');
      const complete=Number.isInteger(result?.riskScore)&&result.riskScore>=0&&result.riskScore<=100;
      output.textContent=complete?result.message+' · '+result.riskScore+'/100':result?.message||message||'暂不可用';
      output.dataset.level=complete?result.level:'unavailable';
      $('.environment-rating').title='在VPS工作用户的Chrome环境检测，每分钟刷新。风险分越低越好，属于非官方估计。'+(result?.username?'\nVPS用户：'+result.username:'')+(result?.signals?.length?'\n'+result.signals.map(s=>s.label+'：'+s.value+'（+'+s.contribution+'）').join('\n'):'');
    }
    function startEnvironmentMonitor(attempt){
      let environmentBusy=false;
      const refresh=async()=>{
        if(environmentBusy||!connectionReady||developerPreview||attempt!==connectionAttempt||document.hidden||$('#workspace-screen').hidden)return;
        environmentBusy=true;
        try{const result=await native('environment.assess');if(attempt===connectionAttempt&&connectionReady&&!developerPreview)setEnvironmentAssessment(result);}
        catch{if(attempt===connectionAttempt&&connectionReady&&!developerPreview)setEnvironmentAssessment(null,'暂不可用');}
        finally{environmentBusy=false;}
      };
      clearInterval(environmentTimer);setEnvironmentAssessment(null,'检测中…');void refresh();environmentTimer=setInterval(refresh,60000);
    }
    function $$(selector){return [...document.querySelectorAll(selector)];}
    queryIpInfo=async function(ip){
      const attempt=connectionAttempt;
      const current=()=>attempt===connectionAttempt&&ip===serverConnection.egressIp;
      if(!isPublicIpv4(ip)){$('#ip-location').textContent='暂无归属地';$('#ip-score').textContent='—';$('#score-unit').textContent='';$('#rating-state').textContent='';return;}
      $('#rating-state').textContent='查询中';$('#ip-location').textContent='正在查询…';$('#ip-score').textContent='—';$('#score-unit').textContent='';
      try{
        const info=await native('ip.info',{ip});
        if(!current())return;
        $('#ip-location').textContent=info.location||'暂无归属地';$('#ip-score').textContent=info.score===null?'—':String(info.score);$('#score-unit').textContent=info.score===null?'':' / 100';$('#rating-state').textContent=info.geoSource;
        $('#score-source').title='定位：'+info.geoSource+(info.hasGeoConflict?'（部分来源存在分歧）':'')+'；评分为第三方'+(info.isSubnetScore?'网段':'IP')+'评分。';
      }catch{if(current()){$('#rating-state').textContent='暂不可用';$('#ip-location').textContent='暂无归属地';$('#ip-score').textContent='—';}}
    };
    let connectionAttempt=0;let connectionReady=false;let connectionInFlight=false;let lastSettings=null;
    function applyDeploymentSettings(settings){
      accountSettingsSaved=settings?.accountSaved===true;
      localeSelection=Object.hasOwn(localePresets,settings?.localeRegion)?settings.localeRegion:'';
    }
    function lockWorkspace(locked){for(const id of ['deployment-panel','maintenance-panel'])$( '#'+id).inert=locked;$('#workspace-screen').classList.toggle('is-connecting',locked);$('#maintenance-mode').disabled=locked;}
    function connectionProgress(text,failed=false){$('#connection-progress').hidden=false;$('#connection-progress').classList.toggle('failed',failed);$('#connection-progress-text').textContent=text;$('#cancel-connect').hidden=failed;$('#retry-connect').hidden=!failed;$('#edit-connection').hidden=!failed;if(failed){busy=false;renderTasks();const values=$$('.network-top strong');for(const value of values)value.textContent='—';$('#rating-state').textContent='未连接';$('#ip-location').textContent='等待连接服务器';$('#server-address').textContent='待确认';}}
    function stopConnectionMonitors(){
      clearInterval(metricsTimer);clearInterval(resourceTimer);clearInterval(environmentTimer);
      clearTimeout(publicGatewayTimer);
    }
    function prepareWorkspace(settings){
      stopConnectionMonitors();
      publicGatewayEnabled=false;publicGatewayReachable=false;publicGatewayLoading=false;publicGatewayMissing=false;
      publicGatewayUpgrade=false;publicGatewayNeedsCloudKey=false;
      bundledGatewayRevision=minimumGatewayRevision;publicGatewayLegacy=false;
      publicGatewayOperation=publicGatewayStatusError='';
      publicGatewayError=undefined;
      publicGatewayCreatedAccount=undefined;
      $('#gateway-repair-dialog').close();
      $('#claude-desktop-dialog').close();
      publicGatewaySubscription=undefined;
      $('#public-gateway-footer').hidden=true;
      setResources(null,'等待服务器连接完成。');setEnvironmentAssessment(null,'等待连接');$('#update-login-password').hidden=true;$('#disconnect-server').hidden=true;nativeServer=null;connectionReady=false;connectionInFlight=true;busy=false;currentTask=-1;accountOptions={};resetDeploymentSettings();ipPolicy={lockIngress:false,lockEgress:false};
      resetHostControl();
      $('#account-settings').close();$('#localization-dialog').close();
      states=Array(tasks.length).fill('pending');taskLogs=Array(tasks.length).fill('等待连接完成。');selectedTasks.clear();selecting=false;renderTasks();
      serverConnection={ingressIp:settings.host,egressIp:''};$('#server-ingress').textContent=settings.host;$('#server-address').textContent='正在检查…';
      $('#ip-location').textContent='等待连接';$('#ip-score').textContent='—';$('#score-unit').textContent='';$('#rating-state').textContent='待检查';setMetrics(null);
      switchMode('deployment');lockWorkspace(true);showScreen('workspace');connectionProgress('正在连接服务器…');
    }
    function confirmServerIdentity(response){const changed=!!response.key.previousFingerprint;
      return appDialog(changed?'检测到服务器密钥已经变化。\n您是否重装过这台服务器？\n\n刚刚重装过，可选择“是”重新信任并连接；没有重装请选择“否”并核实服务器。':'首次连接这台服务器。\n请核对服务器身份，确认后建立连接。',{
            title:changed?'是否重装过系统？':'确认服务器身份',confirm:changed?'是，重新连接':'信任并连接',cancel:changed?'否，取消':'取消',
            details:'服务器：'+response.key.host+':'+response.key.port+(changed?'\n原指纹：'+response.key.previousFingerprint:'')+'\n新指纹：'+response.key.fingerprint});
    }
    async function connect(settings, automatic=false, retries=0){
      clearTimeout(startupRetryTimer);
      const attempt=++connectionAttempt;lastSettings=settings;
      if(automatic){$('#ip').value=settings.host;$('#username').value=settings.username;sshPort=settings.port||22;}
      $('#password').type='password';setEye($('#reveal'),false);prepareWorkspace(settings);
      // 先呈现主界面，再开始 SSH；慢连接、身份确认和评分均留在主界面处理。
      await native('window.resize',{wide:true});await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      try{
        let response;
        for(;;){
          if(attempt!==connectionAttempt)return null;
          response=await native('connect',{host:settings.host,username:settings.username,password:settings.password,port:settings.port||sshPort,automatic});
          if(attempt!==connectionAttempt)return null;
          if(response.retryable&&automatic){
            connectionInFlight=false;
            const delay=Math.min(60, 5 * 2 ** retries);
            connectionProgress('服务器暂不可达，'+delay+' 秒后自动重连。',true);
            $('#cancel-connect').hidden=false;
            startupRetryTimer=setTimeout(()=>{
              if(attempt===connectionAttempt)void connect(settings,true,retries+1);
            },delay*1000);
            return null;
          }
          if(!response.requiresTrust)break;
          if(automatic)throw new Error('服务器身份需要核对，请点击「重试」确认后连接。');
          const changed=!!response.key.previousFingerprint;
          connectionProgress(changed?'等待确认：服务器密钥已变化。':'等待确认服务器身份。');
          const accepted=await confirmServerIdentity(response);
          if(attempt!==connectionAttempt)return null;
          if(!accepted){connectionInFlight=false;await native('connection.cancel');connectionProgress('已取消连接，原信任记录未修改。',true);return null;}
          await native('host.trust',{token:response.token});connectionProgress('正在重新连接服务器…');
        }
        const result=response.server;
        const [saved,draft]=await Promise.all([
          native('deployment.settings.get').catch(()=>null),native('account.draft.get').catch(()=>null)
        ]);
        if(attempt!==connectionAttempt)return null;
        applyDeploymentSettings(saved);accountOptions=draft||{};accountDraftPending=!!draft;
        $('#password').value='';serverConnection={ingressIp:result.ipPolicy?.ingressIp||settings.host,egressIp:result.ipPolicy?.egressIp||result.observedEgressIp||''};$('#server-ingress').textContent=serverConnection.ingressIp;$('#server-address').textContent=serverConnection.egressIp||'待确认';
        taskLogs=Array(tasks.length).fill('');busy=false;connectionReady=true;connectionInFlight=false;lockWorkspace(false);applyServer(result);
        $('#connection-progress').hidden=true;
        let metricsBusy=false;
        const measure=async()=>{
          if(metricsBusy||!connectionReady||attempt!==connectionAttempt)return;
          metricsBusy=true;
          try{const stats=await native('metrics');if(attempt===connectionAttempt)setMetrics(stats);}
          catch{if(attempt===connectionAttempt)setMetrics({latencyMs:null,lossPercent:100});}
          finally{metricsBusy=false;}
        };
        metricsTask=measure();metricsTimer=setInterval(measure,5000);
        startResourceMonitor(attempt);
        startEnvironmentMonitor(attempt);
        // 评分不会阻塞连接完成或部署操作。
        void queryIpInfo(serverConnection.egressIp);
        await pollClaudeBridge();
        if(attempt!==connectionAttempt)return null;
        if(response.gatewayWarnings?.length)void showAppMessage(response.gatewayWarnings.join('\n'),{title:'网关未恢复'});
        if(result.activeJobs?.length)void resumeJob(result.activeJobs[0]);return result;
      }catch(error){
        if(attempt!==connectionAttempt)return null;
        connectionReady=false;connectionInFlight=false;lockWorkspace(true);connectionProgress(error.message,true);
        await showAppMessage(error.message,{title:'连接未完成'});return null;
      }finally{if(attempt===connectionAttempt)resetLogin();}
    }
    $('#cancel-connect').onclick=async()=>{clearTimeout(startupRetryTimer);connectionAttempt++;resetHostControl();connectionInFlight=false;connectionProgress('正在取消连接…');await native('connection.cancel');connectionProgress('已取消连接。',true);};
    $('#edit-connection').onclick=()=>{clearTimeout(startupRetryTimer);connectionAttempt++;resetHostControl();native('connection.cancel');showScreen('login');resetLogin();};
    $('#retry-connect').onclick=()=>{if(lastSettings)void connect(lastSettings);};
    $('#disconnect-server').onclick=async()=>{
      if(!connectionReady||busy)return;
      const button=$('#disconnect-server'), label=button.textContent;
      button.textContent='正在退出…';
      busy=true;lockWorkspace(true);renderTasks();
      try{
        await native('connection.disconnect');
        connectionAttempt++;
        connectionReady=false;nativeServer=null;lastSettings=null;sshPort=22;
        accountOptions={};resetDeploymentSettings();resetHostControl();
        clearTimeout(startupRetryTimer);stopConnectionMonitors();
        advancedOperation=null;displayedOperation=null;operationHistory.clear();
        $('#cloud-live-log').hidden=true;$('#mount-live-log').hidden=true;
        $('#mount-summary').textContent='将本机目录挂载到服务器，支持多组目录。';
        window.safeCodeMountStatus({count:0,message:'未挂载目录'});
        $('#public-gateway-footer').hidden=true;
        $('#ssh-port').setCustomValidity('');
        returnToServerLogin();
      }catch(error){
        void showAppMessage(error.message,{title:'尚未退出服务器'});
      }finally{
        button.textContent=label;
        busy=false;lockWorkspace(false);renderTasks();
      }
    };
    $('#login-form').onsubmit=async e=>{
      e.preventDefault();
      const host=$('#ip').value.trim();
      const username=$('#username').value.trim();
      const password=$('#password').value;
      if(isDeveloperHost(host)){
        try{await beginPreview();}catch(error){await showAppMessage(error.message);}return;
      }
      if(!host||!username){
        $('#connection-message').textContent='请填写 IP 地址和用户名。';
        $('#connection-message').className='connection-message error';
        return;
      }
      await connect({host,username,password,port:sshPort});
    };
    function inputRemovalMessage(server){
      return server.inputPinyinInstalled
        ? '已卸载当前工作用户的中文输入法配置，系统拼音包仍保留。重新登录远程桌面后生效。'
        : '中文输入法已卸载，重新登录远程桌面后生效。';
    }
    async function resumeJob(job){
      busy=true;
      const removal=job.operation==='input_remove';
      const advanced=job.operation==='cloud'||job.operation==='mount_install';
      if(advanced)beginOperation(job.operation,job.operation==='cloud'?'恢复 CloudCLI 安装进度':'恢复目录挂载安装进度');
      currentTask=ids.indexOf(removal?'input':job.operation);
      if(currentTask>=0){states[currentTask]='running';taskLogs[currentTask]='恢复远端部署监控…';}renderTasks();
      try{
        const server=await native('job.resume',job);applyServer(server);
        if(currentTask>=0){
          states[currentTask]=server.statuses?.[ids[currentTask]]?'done':'pending';
          taskLogs[currentTask]=removal?inputRemovalMessage(server):'部署完成，已检查服务器状态。';
        }
        if(advanced){appendOperationLog('后台安装任务已完成。');advancedOperation.status='安装完成。';}
      }catch(error){
        if(advanced){appendOperationLog('安装未完成：'+error.message);advancedOperation.status='安装未完成，请查看日志后重试。';}
        else{
          if(currentTask>=0){
            states[currentTask]=nativeServer?.statuses?.[ids[currentTask]]?'done':'pending';
            taskLogs[currentTask]=error.message;
          }
          showAppMessage(error.message);
        }
      }finally{
        if(advanced){advancedOperation.done=true;if(displayedOperation===advancedOperation)$('#operation-log-status').textContent=advancedOperation.status;}
        currentTask=-1;busy=false;renderTasks();
      }
    }
    async function runTask(index,removal=false){
      if(!connectionReady||(!removal&&!deploymentSettingsReady()))throw new Error('请先保存工作用户和本土化设置。');
      const previousState=states[index];currentTask=index;states[index]='running';taskLogs[index]='正在连接 Ubuntu 部署模块…';renderTasks();
      try{
        const preset=currentLocale();
        const locale=preset?{region:preset.name,timezone:preset.timezone,locale:preset.locale,language:preset.language}:null;
        const step=removal?'input_remove':ids[index];
        const result=await native('deploy',removal?{step}:{step,locale,username:accountOptions.username||nativeServer?.username||'workspace',hostname:accountOptions.hostname||'',password:accountOptions.password||''});
        applyServer(result);
        if(removal){
          if(result.inputManaged||result.statuses?.input)throw new Error('中文输入法卸载验证未通过，请查看日志。');
          states[index]='pending';taskLogs[index]=inputRemovalMessage(result);
        }else{
          if(!result.statuses?.[step])throw new Error(tasks[index][0]+'验证未通过，请查看日志。');
          taskLogs[index]='部署完成，已通过实际状态检查。';
          if(index===0){accountOptions={};accountDraftPending=false;}
        }
      }catch(error){
        states[index]=removal||index===0?previousState:'pending';
        taskLogs[index]=error.message;
        throw error;
      }finally{currentTask=-1;renderTasks();}
    }
    deployOne=async function(index){if(busy||!connectionReady||!deploymentSettingsReady()||!canDeployTask(index))return;selecting=false;selectedTasks.clear();busy=true;try{await runTask(index);}catch(error){showAppMessage(error.message);}finally{busy=false;renderTasks();}};
    removeChineseInput=async()=>{
      if(busy||!connectionReady||!inputManaged)return;
      const attempt=connectionAttempt;
      const confirmed=await showAppMessage('将卸载当前工作用户的 SafeCode 中文输入法。中文字体、词库和其他输入法会保留。卸载后请重新登录远程桌面。',{title:'卸载中文输入法',confirm:'卸载',cancel:'取消'});
      if(!confirmed||busy||attempt!==connectionAttempt||!connectionReady||!inputManaged)return;
      busy=true;
      try{await runTask(ids.indexOf('input'),true);}catch(error){showAppMessage(error.message);}
      finally{busy=false;renderTasks();}
    };
    $('#deploy-all').onclick=async()=>{
      if(busy||!connectionReady||!deploymentSettingsReady())return;if(!selecting){selecting=true;selectedTasks.clear();renderTasks();return;}
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
      $('#disconnect-server').disabled=busy;
      $('#update-login-password').disabled=busy;
      if(document.querySelector('#bridge-manage'))$('#bridge-manage').disabled=busy||bridgeBusy||(!isMac&&!nativeServer?.statuses?.account);
      if(document.querySelector('#host-manage'))$('#host-manage').disabled=busy||hostBusy||!nativeServer?.statuses?.account;
      renderDesktopAccess();
      for(const [key,ready] of [['mount',nativeServer?.mountReady],['cloud',nativeServer?.cloudReady]]){const button=$('#install-'+key);button.disabled=!!ready||busy;button.title=busy?'有任务正在执行，请查看进度。':'';button.textContent=advancedOperation&&!advancedOperation.done&&advancedOperation.key===(key==='cloud'?'cloud':'mount_install')?'安装中…':ready?'已安装':'安装';}
      if($('#open-cloud-remote'))$('#open-cloud-remote').disabled=!nativeServer?.cloudReady||busy;
      const checkingCloudLogin=cloudLoginAttempt===connectionAttempt;
      $('#cloud-login').disabled=!connectionReady||!nativeServer?.cloudReady||busy||checkingCloudLogin;
      $('#cloud-login').textContent=checkingCloudLogin?'查询中…':'查询密码';
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
    $('#cloud-login').onclick=async()=>{
      if($('#cloud-login').disabled)return;
      const attempt=connectionAttempt;
      const showIf=()=>attempt===connectionAttempt&&connectionReady&&!busy;
      cloudLoginAttempt=attempt;
      syncAdvancedButtons();
      try{
        const login=await native('cloud.login');
        if(showIf())await showCloudCliLogin(login,{showIf});
      }catch{
        if(showIf())await showAppMessage('暂时无法查询 CloudCLI 登录信息，请确认服务器连接后重试。',{showIf});
      }finally{
        if(cloudLoginAttempt===attempt){cloudLoginAttempt=null;syncAdvancedButtons();}
      }
    };
    $('#open-cloud').onclick=()=>native('cloud.open').catch(e=>showAppMessage(e.message));
    let cloudRemoteUrl=null;
    let cloudRemoteRunning=false;
    function appendCloudRemoteLog(line){
      const box=$('#cloud-remote-log-output');
      if(box){box.textContent=(box.textContent?box.textContent+'\n':'')+line;box.scrollTop=box.scrollHeight;}
    }
    const remoteConfirm=$('#cloud-remote-confirm');
    if(remoteConfirm)remoteConfirm.onclick=async()=>{
      $('#cloud-remote-warning-view').hidden=true;
      $('#cloud-remote-log-view').hidden=false;
      $('#cloud-remote-success-view').hidden=true;
      $('#cloud-remote-log-status').textContent='正在配置并开放公网访问…';
      $('#cloud-remote-log-output').textContent='已确认风险，正在连接服务器执行配置…\n';
      cloudRemoteRunning=true;busy=true;renderTasks();
      try{
        const res=await native('cloud.expose');
        const url=(res&&res.url)?res.url:('http://'+(res?.ip||serverConnection.ingressIp)+':3001');
        cloudRemoteUrl=url;
        window.showCloudRemoteSuccess?.(url);
      }catch(err){
        $('#cloud-remote-log-status').textContent='开放公网访问失败：'+err.message;
        appendCloudRemoteLog('错误：'+err.message);
      }finally{
        cloudRemoteRunning=false;busy=false;renderTasks();
      }
    };
    if($('#open-cloud-remote'))$('#open-cloud-remote').onclick=()=>{
      if(cloudRemoteUrl){window.openCloudRemoteDialog?.(cloudRemoteUrl);}
      else{window.openCloudRemoteDialog?.();}
    };
    let selectedRemoteField=null;let folderEpoch=0;let lastFolderPath='';let folderTimer;
    function appendFolderLog(line){const box=$('#folder-log');box.textContent=(box.textContent+'\n'+line).trim().split('\n').slice(-20).join('\n');box.scrollTop=box.scrollHeight;}
    async function listRemote(path=''){
      const epoch=++folderEpoch;lastFolderPath=path;clearInterval(folderTimer);$('#folder-log').textContent='';$('#folder-error').textContent='';$('#folder-list').textContent='正在读取…';$('#folder-confirm').disabled=true;$('#folder-new').disabled=true;$('#folder-up').disabled=true;$('#folder-retry').hidden=true;
      $('#folder-path').textContent=path||(nativeServer?.homeDirectory?nativeServer.homeDirectory+'/workspace':'工作目录');appendFolderLog('正在通过 SSH 读取工作用户目录…');const started=Date.now();
      folderTimer=setInterval(()=>appendFolderLog('仍在等待服务器响应，已用时 '+Math.floor((Date.now()-started)/1000)+' 秒。'),3000);
      try{
        const result=await native('folder.list',{path});if(epoch!==folderEpoch)return;
        lastFolderPath=result.path;$('#folder-path').textContent=result.path;$('#folder-up').disabled=result.path===result.parent;$('#folder-up').onclick=()=>listRemote(result.parent);$('#folder-list').replaceChildren();
        for(const dir of result.directories){const button=document.createElement('button');button.textContent=dir.name;button.onclick=()=>listRemote(dir.path);$('#folder-list').append(button);}
        if(!result.directories.length)$('#folder-list').textContent='此目录没有下级文件夹。';$('#folder-confirm').disabled=false;$('#folder-new').disabled=false;appendFolderLog('读取完成：'+result.directories.length+' 个目录。'+(result.truncated?'仅显示前 300 个，可手动填写更具体路径。':''));
      }catch(e){if(epoch!==folderEpoch)return;$('#folder-list').textContent='';$('#folder-error').textContent=e.message;appendFolderLog('读取未完成，可点击重试或返回上一级。');$('#folder-retry').hidden=false;const current=$('#folder-path').textContent;if(current.startsWith('/')&&current!=='/'){const parent=current.slice(0,current.lastIndexOf('/'))||'/';$('#folder-up').disabled=false;$('#folder-up').onclick=()=>listRemote(parent);}}
      finally{if(epoch===folderEpoch)clearInterval(folderTimer);}
    }
    let creatingFolder=false;
    $('#folder-new').onclick=()=>{if($('#folder-confirm').disabled)return;$('#create-folder-name').value='';$('#create-folder-error').textContent='';$('#create-folder-parent').textContent='创建位置：'+$('#folder-path').textContent;$('#create-folder-dialog').showModal();$('#create-folder-name').focus();};
    $('#create-folder-dialog').addEventListener('cancel',event=>{if(creatingFolder)event.preventDefault();});
    $('#create-folder-form').onsubmit=async event=>{
      event.preventDefault();if(creatingFolder)return;const name=$('#create-folder-name').value.trim();
      if(!name||name==='.'||name==='..'||/[\\/\x00-\x1f]/.test(name)){$('#create-folder-error').textContent='请输入有效的文件夹名称，不能包含路径分隔符。';return;}
      creatingFolder=true;const button=$('#create-folder-form [type=submit]');const controls=[...$('#create-folder-dialog').querySelectorAll('button,input')];for(const control of controls)control.disabled=true;button.textContent='正在创建…';
      try{const result=await native('folder.create',{path:$('#folder-path').textContent,name});if(selectedRemoteField)selectedRemoteField.value=result.path;$('#create-folder-dialog').close();$('#remote-folder-dialog').close();}
      catch(error){$('#create-folder-error').textContent=error.message;}
      finally{creatingFolder=false;for(const control of controls)control.disabled=false;button.textContent='创建并选择';}
    };
    window.safeCodeMountStatus=status=>{
      const button=$('#mount-guard-status');button.textContent=status.count?'挂载 '+status.count+' 个目录 · '+(status.state==='connected'?'守护中':status.state==='attention'?'需要处理':'重连中'):'挂载守护 · 未挂载';button.title=status.message;
      button.classList.toggle('inactive',!status.count||status.state!=='connected'||status.authenticationRequired);
      if(status.authenticationRequired)$('#update-login-password').classList.add('needs-credentials');else $('#update-login-password').classList.remove('needs-credentials');
      if(status.count)$('#mount-summary').textContent='已挂载 '+status.count+' 个目录，'+(status.state==='connected'?'断线自动恢复。':status.message+'。');
    };
    $('#mount-guard-status').onclick=()=>native('mount.status.open').catch(error=>showAppMessage(error.message));
    $('#folder-retry').onclick=()=>listRemote(lastFolderPath);
    $('#remote-folder-dialog').addEventListener('close',()=>{folderEpoch++;clearInterval(folderTimer);native('folder.cancel').catch(()=>{});});
    $('#folder-confirm').onclick=()=>{if(selectedRemoteField)selectedRemoteField.value=$('#folder-path').textContent;$('#remote-folder-dialog').close();};
    const originalAppendMountPair=appendMountPair;appendMountPair=function(pair){originalAppendMountPair(pair);const row=$('#mount-list').lastElementChild;for(const key of ['local','remote']){const field=row.querySelector('[data-path='+key+']');if(key==='remote'&&nativeServer?.homeDirectory)field.placeholder=nativeServer.homeDirectory+'/workspace/projects';const wrap=document.createElement('span');wrap.className='folder-input';field.replaceWith(wrap);wrap.append(field);const picker=document.createElement('button');picker.type='button';picker.textContent='选择目录';picker.onclick=async()=>{if(key==='local'){try{const selected=await native('pickFolder');if(selected)field.value=selected;}catch(e){showAppMessage(e.message);}}else{selectedRemoteField=field;$('#remote-folder-dialog').showModal();await listRemote();}};wrap.append(picker);}};
    const localeForm=$('#localization-form');
    const localeRegion=$('#locale-region');
    localeForm.onsubmit=async event=>{
      event.preventDefault();
      const region=localeRegion.value;
      if(!connectionReady||busy||localeSaving||!Object.hasOwn(localePresets,region))return;
      localeSaving=true;localeRegion.disabled=true;$('#close-localization').disabled=true;updateLocalePreview();
      const attempt=connectionAttempt;
      try{
        const saved=await native('deployment.locale.save',{region});
        if(attempt!==connectionAttempt)return;
        if(saved?.localeRegion!==region)throw new Error('服务器设置未保存，请重试。');
        applyDeploymentSettings(saved);taskLogs[1]='';selectedTasks.delete(1);applyServer(nativeServer);
        $('#localization-dialog').close();
      }catch(error){if(attempt===connectionAttempt)await showAppMessage(error.message);}
      finally{localeSaving=false;localeRegion.disabled=false;$('#close-localization').disabled=false;updateLocalePreview();}
    };
    openAccountSettings=async function(){
      const attempt=connectionAttempt;
      try{const draft=await native('account.draft.get',isMac?{interactive:true}:{});if(attempt!==connectionAttempt)return;accountOptions=draft||{};accountDraftPending=!!draft;
        $('#custom-hostname').value=draft?.hostname||nativeServer?.hostname||'safe-workstation';$('#custom-username').value=draft?.username||nativeServer?.username||'workspace';$('#custom-password').type='password';$('#custom-password').value=draft?.password||'';validateWorkPassword();setEye($('#account-settings [data-reveal]'),false);
        $('#account-custom-hint').textContent='工作密码用于工作用户和远程桌面。保存工作用户和本土化设置后即可部署。更换用户名会创建新环境，保留旧项目，需先注销旧桌面并卸载目录，旧 CloudCLI 将停止。';$('#account-settings').showModal();
      }catch(e){showAppMessage(e.message);}
    };
    const workPassword=$('#custom-password');
    function validateWorkPassword(){
      const password=workPassword.value;
      workPassword.setCustomValidity(password && (password.length<12||password.length>256||password.includes('\0'))
        ? '工作密码须为 12–256 位，不能包含空字符。' : '');
    }
    workPassword.addEventListener('input',validateWorkPassword);
    let accountSaving=false;
    $('#account-settings-form').onsubmit=async e=>{
      e.preventDefault();if(!connectionReady||busy||accountSaving||!$('#account-settings-form').reportValidity())return;
      accountSaving=true;const save=$('#account-settings-form [type=submit]');save.disabled=true;
      const attempt=connectionAttempt;
      const draft={username:$('#custom-username').value.trim(),hostname:$('#custom-hostname').value.trim(),password:$('#custom-password').value};
      try{
        const status=await native('account.change.status');
        if(status?.requiresConfirmation){
          const confirmed=await appDialog('频繁更改工作用户名或密码，可能导致远程桌面、SSH 或正在使用的服务连接失败。请确认你了解更改的影响。\n\n这里修改的是工作用户，不是 root 的服务器登录密码。',{
            title:'确认更改工作账户',cancel:'我不改了',confirm:currentLocale()?'我知道自己在做什么，更改并应用':'确认并保存参数'});
          if(!confirmed)return;
        }
        if(attempt!==connectionAttempt)return;
        await native('account.draft.save',draft);
        if(attempt!==connectionAttempt)return;
        accountSettingsSaved=true;accountOptions=draft;accountDraftPending=true;$('#custom-password').value='';$('#account-settings').close();
        taskLogs[0]='参数已保存，等待'+(states[0]==='done'?'更新':'开始部署')+'后应用。';renderTasks();
        if(status?.requiresConfirmation&&deploymentSettingsReady())await deployOne(0);
      }catch(e){await showAppMessage(e.message);}
      finally{accountSaving=false;save.disabled=false;}
    };
    let credentialSaving=false;
    $('#update-login-password').onclick=async()=>{
      if(busy||credentialSaving)return;
      try{const info=await native('connection.info');if(!info)throw new Error('请先登录服务器。');
        $('#credential-target').textContent=info.username+' @ '+info.host+':'+info.port;
        $('#credential-password').value='';$('#credential-password').type='password';setEye($('#credential-form [data-reveal]'),false);$('#credential-error').textContent='';$('#credential-dialog').showModal();$('#credential-password').focus();
      }catch(error){showAppMessage(error.message);}
    };
    $('#close-credential').onclick=()=>{if(!credentialSaving)$('#credential-dialog').close();};
    $('#credential-dialog').addEventListener('cancel',event=>{if(credentialSaving)event.preventDefault();});
    $('#credential-dialog').addEventListener('close',()=>{$('#credential-password').value='';});
    $('#credential-form').onsubmit=async event=>{
      event.preventDefault();if(credentialSaving||busy||!$('#credential-form').reportValidity())return;
      credentialSaving=true;busy=true;renderTasks();
      const controls=[...$('#credential-form').querySelectorAll('input,button'),$('#close-credential')];for(const control of controls)control.disabled=true;
      $('#credential-error').textContent='正在验证新密码并恢复连接…';
      try{
        const password=$('#credential-password').value;$('#credential-password').value='';
        const result=await native('connection.reauthenticate',{password});if(lastSettings)lastSettings.password=password;
        window.safeCodeMountStatus(result.mount);$('#credential-dialog').close();
        void refreshPublicGateway();
        await showAppMessage(result.mount.authenticationRequired?'新密码已验证；挂载仍需处理，请查看挂载守护状态。':'新密码已验证，登录信息已更新。'+(result.mount.count?'挂载状态：'+result.mount.message+'。':''),{title:'登录密码已更新'});
      }catch(error){$('#credential-error').textContent=error.message+' 原挂载和缓存已保留，可重新输入密码。';}
      finally{credentialSaving=false;busy=false;for(const control of controls)control.disabled=false;renderTasks();}
    };
    if(isMac){
      $('#mac-rdp-options').hidden=false;
      $('#rdp-display-hint').textContent='下次打开桌面时生效。使用 SDL FreeRDP 可免重复输入工作用户密码；其他客户端按其自身设置连接。';
    }
    $('#desktop-resolution').onclick=async()=>{
      try{
        const value=await native('desktop.resolution.get');
        $('#resolution-select').value=value.width+'x'+value.height;
        if(isMac){
          $('#rdp-display-mode').value=value.displayMode||'Original';
          $('#rdp-fullscreen').checked=!!value.fullscreen;
        }
        $('#resolution-dialog').showModal();
      }catch(error){showAppMessage(error.message);}
    };
    $('#resolution-form').onsubmit=async event=>{
      event.preventDefault();
      const [width,height]=$('#resolution-select').value.split('x').map(Number);
      const settings={width,height};
      if(isMac)Object.assign(settings,{displayMode:$('#rdp-display-mode').value,fullscreen:$('#rdp-fullscreen').checked});
      try{
        const result=await native('desktop.resolution.save',settings);
        $('#resolution-dialog').close();
        if(result.deferred>0)showAppMessage(isMac?'已保存，下次打开远程桌面时生效。':'窗口大小已调整。当前服务器未接受在线分辨率切换，新像素分辨率将在下次打开桌面时使用。');
      }catch(error){showAppMessage(error.message);}
    };
    function fillSavedLogin(saved){
      if(!saved||!$('#workspace-screen').hidden)return;
      $('#ip').value=saved.host;$('#username').value=saved.username;$('#password').value=saved.password;sshPort=saved.port||22;
    }
    const restore=$('#restore-saved-login');
    async function loadLoginForm(){
      const attempt=connectionAttempt;
      try{
        const saved=await native('login.load');
        if(attempt!==connectionAttempt)return;
        if(saved){
          if(!['#ip','#username','#password'].some(id=>$(id).value))fillSavedLogin(saved);
          return;
        }
      }catch{}
      try{restore.hidden=!(await native('login.restoreAvailable'));}catch{}
      if(!restore.hidden&&isMac)restore.textContent='解锁已保存的登录信息';
    }
    void loadLoginForm();
    restore.onclick=async()=>{
      restore.disabled=true;
      try{
        const login=await native('login.restoreSaved');
        if(!login)throw new Error('暂时无法读取已保存的登录信息。');
        fillSavedLogin(login);
        restore.hidden=true;
        const saved=await native('gateways.resume-settings');
        if(saved&&!$('#login-screen').hidden&&!connectionInFlight)await connect(saved,true);
      }catch(error){await showAppMessage(error.message);}
      finally{restore.disabled=false;}
    };
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
      try{const result=await native('mount.apply',{mappings});mountPairs=mappings;$('#mount-summary').textContent=mappings.length?'已挂载 '+mappings.length+' 组目录。':'已卸载所有目录。';$('#mount-dialog').close();if(result?.cloudWarning)showAppMessage(result.cloudWarning);}catch(error){showAppMessage(error.message);}
      finally{clearInterval(timer);mountSaving=false;busy=false;mountProgress.hidden=true;form.removeAttribute('aria-busy');for(const [control,disabled] of controls)control.disabled=disabled;button.textContent='保存设置';renderTasks();}
    };
    function renderGatewayBadge(id,name,enabled){const badge=$('#'+id);badge.textContent=name+'网关'+(enabled?'已启用':'未启用');badge.classList.toggle('inactive',!enabled);}
    const openingClients=new WeakSet();
    const codexWarning='第三方功能，非本人创作，未经本人测试。使用请自行承担后果。';
    function setLaunchAvailability(id,ready){
      const button=$('#'+id);button.disabled=!ready||openingClients.has(button);
    }
    function renderClaudeBridge(value){
      if(isMac){$('#bridge-state').textContent='暂不可用';$('#bridge-summary').textContent='当前附件仅有 Windows 适配器，macOS 暂不可用。';$('#bridge-manage').disabled=busy;$('#bridge-start').disabled=true;setLaunchAvailability('codex-launch',false);return;}
      if(value)bridgeStatus=value;const running=!!bridgeStatus?.running,ready=running&&!!bridgeStatus?.configuration;
      setLaunchAvailability('codex-launch',running&&!!bridgeStatus?.connected&&!bridgeBusy);
      renderGatewayBadge('codex-gateway-status','Codex',running&&!!bridgeStatus?.connected);
      $('#bridge-state').textContent=running?(bridgeStatus.connected?'已连接':'连接中'):bridgeStatus?.cleanupPending?'清理待重试':'未启动';
      $('#bridge-summary').textContent=running?(bridgeStatus.message||'本机适配器运行中'):'习惯 Codex 客户端？在里面连接服务器上的 Claude。';
      $('#bridge-manage').disabled=bridgeBusy||busy||!nativeServer?.statuses?.account;$('#bridge-close').disabled=bridgeBusy;
      for(const id of ['bridge-model','bridge-port','bridge-write','bridge-consent'])$('#'+id).disabled=bridgeBusy||running;
      if(running)$('#bridge-consent').checked=true;
      $('#bridge-start').hidden=running;$('#bridge-start').disabled=bridgeBusy||!$('#bridge-consent').checked;$('#bridge-start').textContent=bridgeBusy?'正在部署…':'部署并启动';
      $('#bridge-stop').hidden=!running&&!bridgeStatus?.cleanupPending;$('#bridge-stop').disabled=bridgeBusy;$('#bridge-stop').textContent=bridgeStatus?.cleanupPending&&!running?'重试清理':'停止适配器';
      $('#bridge-config-area').hidden=!ready;$('#bridge-config').value=bridgeStatus?.configuration||'';$('#bridge-url').textContent='http://127.0.0.1:'+(bridgeStatus?.port||18797)+'/v1';
      $('#bridge-copy-config').disabled=!ready||bridgeBusy;$('#bridge-copy-key').disabled=!ready||bridgeBusy;$('#bridge-dialog-state').textContent=$('#bridge-state').textContent;
    }
    window.safeCodeClaudeBridgeStatus=renderClaudeBridge;
    async function pollClaudeBridge(){
      if(bridgePolling||bridgeBusy||!nativeServer)return;
      const attempt=connectionAttempt;
      bridgePolling=true;
      try{const status=await native('bridge.status');if(attempt===connectionAttempt)renderClaudeBridge(status);}
      catch{}finally{bridgePolling=false;}
    }
    $('#bridge-manage').onclick=async()=>{
      if(isMac){
        await showAppMessage(codexWarning+'\n\n当前适配器仅支持 Windows，macOS 暂不可用。',{title:'第三方功能警告'});
        return;
      }
      if(!await showAppMessage(codexWarning,{title:'第三方功能警告',confirm:'继续设置',cancel:'取消'}))return;
      await pollClaudeBridge();$('#bridge-consent').checked=false;$('#bridge-model').value=bridgeStatus?.model||'sonnet';$('#bridge-port').value=bridgeStatus?.port||18797;$('#bridge-write').checked=!!bridgeStatus?.writable;
      $('#bridge-progress').textContent=bridgeStatus?.running?bridgeStatus.message:'配置与现有供应商分开保存。启动只检查接口和 SSH，不会自动发送模型请求。';renderClaudeBridge();$('#bridge-dialog').showModal();
    };
    $('#bridge-close').onclick=()=>{if(!bridgeBusy)$('#bridge-dialog').close();};$('#bridge-dialog').addEventListener('cancel',event=>{if(bridgeBusy)event.preventDefault();});$('#bridge-consent').onchange=()=>renderClaudeBridge();
    $('#bridge-form').onsubmit=async event=>{
      event.preventDefault();if(bridgeBusy||!$('#bridge-consent').checked||!$('#bridge-form').reportValidity())return;
      bridgeBusy=true;renderClaudeBridge();$('#bridge-progress').textContent='正在校验第三方组件、准备专用 SSH 授权并启动本机接口…';
      try{bridgeStatus=await native('bridge.start',{port:Number($('#bridge-port').value),model:$('#bridge-model').value.trim().toLowerCase(),writable:$('#bridge-write').checked,thirdPartyAccepted:true});$('#bridge-progress').textContent=bridgeStatus.message+'。可复制配置与本机密钥到 CC Switch；模型调用尚未自动测试。';}
      catch(error){$('#bridge-consent').checked=false;$('#bridge-progress').textContent=error.message;}
      finally{bridgeBusy=false;renderClaudeBridge();}
    };
    $('#bridge-stop').onclick=async()=>{
      if(bridgeBusy)return;bridgeBusy=true;renderClaudeBridge();$('#bridge-progress').textContent='正在关闭本机接口并撤销专用授权，尝试取消本机发起的远端任务…';
      try{bridgeStatus=await native('bridge.stop');$('#bridge-consent').checked=false;$('#bridge-progress').textContent=bridgeStatus.cleanupPending?'本机接口已停止，远端清理尚未确认，可恢复连接后重试。':'适配器已停止，专用授权已移除。';}
      catch(error){$('#bridge-progress').textContent=error.message;}
      finally{bridgeBusy=false;renderClaudeBridge();}
    };
    $('#bridge-copy-config').onclick=async()=>{try{await native('bridge.copy-config');$('#bridge-progress').textContent='供应商配置已复制，不包含密钥。请在 CC Switch 中新增 Codex 供应商。';}catch(error){$('#bridge-progress').textContent=error.message;}};
    $('#bridge-copy-key').onclick=async()=>{try{await native('bridge.copy-key');$('#bridge-progress').textContent='本机适配器密钥已复制。只填写到本机 CC Switch/Codex，勿发给他人。';}catch(error){$('#bridge-progress').textContent=error.message;}};
    setInterval(()=>{if(!$('#workspace-screen').hidden)pollClaudeBridge();},4000);

    function connectionLabel(){
      return keyState.running&&keyState.gatewayConnected?'连通成功':'连通失败';
    }
    function renderDesktopAccess(){
      const ready=connectionReady&&!!nativeServer?.statuses?.account;
      const checking=publicGatewayLoading&&!publicGatewayReachable&&!publicGatewayStatusError&&!publicGatewayError;
      const unavailable=!ready||busy||!!publicGatewayOperation;
      $('#claude-gateway-status').textContent=connectionLabel();
      $('#claude-gateway-status').classList.toggle('inactive',!(keyState.running&&keyState.gatewayConnected));
      const updateAvailable=publicGatewayReachable&&publicGatewayUpgrade;
      $('#claude-desktop-manage').textContent=updateAvailable?'网关设置 · 有更新':'网关设置';
      $('#claude-desktop-manage').disabled=!ready;
      const updateButton=$('#safe-code-api-update');
      updateButton.hidden=!updateAvailable;
      updateButton.type=updateAvailable?'submit':'button';
      updateButton.disabled=unavailable||!updateAvailable;
      $('#public-gateway-runtime').disabled=!ready;
      $('#safe-code-api-enable').disabled=unavailable||checking||publicGatewayEnabled||updateAvailable;
      $('#safe-code-api-enable').textContent=publicGatewayEnabled?'已启用':'启用';
      $('#safe-code-api-manage').disabled=unavailable||!publicGatewayEnabled||publicGatewayLegacy;
      $('#safe-code-api-repair').disabled=unavailable||publicGatewayMissing;
      $('#safe-code-cloudcli-config').hidden=!publicGatewayNeedsCloudKey;
      $('#safe-code-cloudcli-api-key').disabled=!!publicGatewayOperation;
      $('#claude-desktop-dialog-state').textContent=publicGatewayOperation?'处理中…':checking?'正在读取…':
        !publicGatewayReachable?'状态待确认':publicGatewayEnabled?'公网已启用':publicGatewayUpgrade?'待升级':
        publicGatewayMissing?'待安装':'未启用';
      $('#safe-code-api-progress').textContent=gatewayInstructions(ready,checking);
    }
    function gatewayInstructions(ready,checking){
      if(publicGatewayOperation)return publicGatewayOperation+' 可以关闭此窗口，稍后从「网关设置」查看。';
      if(!ready)return '服务器连接尚未就绪。请关闭窗口，重新连接服务器。';
      if(busy)return '服务器正在执行其他任务。请关闭窗口，任务完成后再打开「网关设置」。';
      if(checking)return '正在读取公网网关状态。可以关闭此窗口。';
      if(publicGatewayError){
        if(publicGatewayError.action==='manage')return publicGatewayError.message+
          (publicGatewayEnabled?' 点击「管理网关」重试。':' 点击「启用」重新检查公网网关。');
        return publicGatewayError.message;
      }
      if(!publicGatewayReachable&&publicGatewayStatusError)return publicGatewayStatusError+
        (publicGatewayEnabled?' 点击「管理网关」重试，也可以关闭窗口后检查 SSH 连接。':' 检查 SSH 连接后，点击「启用」重试。');
      if(publicGatewayNeedsCloudKey)return '填写下方的 CloudCLI API Key，再点击「'+(publicGatewayUpgrade?'更新网关':'启用')+'」。';
      if(publicGatewayUpgrade)return '当前客户端包含新版网关。点击「更新网关」更新 VPS，保留已有 API Key。';
      if(publicGatewayEnabled)return '点击「管理网关」创建 API Key，再回到主界面粘贴。';
      if(publicGatewayMissing)return '点击「启用」安装并开放公网网关。首次安装可能需要几分钟。';
      return '点击「启用」开放公网网关。';
    }
    function setPublicGatewayOperation(message){
      publicGatewayOperation=message;
      renderDesktopAccess();
    }
    function readPublicGatewayStatus(){
      return native('safeCodeApi.publicStatus',{},{
        ms:12000,message:'读取公网网关状态超时。'
      });
    }
    function renderPublicGatewayStatus(status,error=''){
      const footer=$('#public-gateway-footer'),badge=$('#public-gateway-runtime'),quota=$('#footer-claude-limits');
      footer.hidden=!connectionReady||!nativeServer?.statuses?.account;
      const reachable=Boolean(status);
      if(reachable) {
        publicGatewayMissing=status.installed===false;
        if(Number.isInteger(status.bundledGatewayRevision))bundledGatewayRevision=status.bundledGatewayRevision;
        publicGatewayLegacy=!publicGatewayMissing&&(status.backend!=='cloudcli'||(status.gatewayRevision??0)<minimumGatewayRevision);
        publicGatewayUpgrade=!publicGatewayMissing&&(publicGatewayLegacy||(status.gatewayRevision??0)<bundledGatewayRevision);
        publicGatewayEnabled=status.enabled===true;
        if(status.cloudcliConfigured===true||(publicGatewayEnabled&&!publicGatewayLegacy))publicGatewayNeedsCloudKey=false;
        if(!publicGatewayUpgrade&&(publicGatewayError?.action==='update'||(publicGatewayEnabled&&publicGatewayError?.action==='enable')))publicGatewayError=undefined;
      }
      publicGatewayReachable=reachable;
      publicGatewayStatusError=error;
      badge.classList.toggle('inactive',!reachable||!publicGatewayEnabled);
      badge.title=error||(publicGatewayEnabled?'打开 Safe Code API 网关管理':'打开 Safe Code API 公网设置');
      if(!reachable)badge.textContent=/SSH 认证失败|服务器身份已变化/.test(error||'')
        ?'公网网关 · 需检查 SSH':publicGatewayEnabled?'公网网关 · 管理连接重连中':'公网网关 · 状态不可用';
      else if(publicGatewayMissing)badge.textContent='公网网关 · 待安装';
      else if(publicGatewayUpgrade)badge.textContent='公网网关 · 待升级';
      else if(!publicGatewayEnabled)badge.textContent='公网网关 · 未启用';
      else if(status.waiting==null)badge.textContent='公网网关 · 已启用';
      else {
        const limit=String(status.concurrency_text??status.concurrency);
        badge.textContent='公网网关 · 并发 '+status.active+(limit==='0'?'（不限）':'/'+limit)+' · 排队 '+status.waiting;
      }
      if(isMac){renderDesktopAccess();return;}
      if(reachable)publicGatewaySubscription=status.subscription;
      quota.hidden=!publicGatewayEnabled;
      renderClaudeSubscription(quota,!reachable&&publicGatewaySubscription
        ?{...publicGatewaySubscription,state:'stale'}:publicGatewaySubscription);
      renderDesktopAccess();
    }
    if(!isMac)bindClaudeUsageRefresh($('#footer-claude-limits'), async()=>{
      const attempt=connectionAttempt;
      const value=await native('safeCodeApi.refreshSubscription');
      if(attempt!==connectionAttempt)throw new Error('服务器连接已变更，请重新查询。');
      return value;
    },value=>{
      publicGatewaySubscription=value;
      renderClaudeSubscription($('#footer-claude-limits'),value);
    });
    function refreshPublicGateway(interactive=false){
      const attempt=connectionAttempt;
      if(publicGatewayOperation||!connectionReady||!nativeServer?.statuses?.account)return;
      clearTimeout(publicGatewayTimer);
      if(interactive){
        publicGatewayLoading=true;
        renderDesktopAccess();
      }
      if(publicGatewayTask?.attempt===attempt)return publicGatewayTask.promise;
      const promise=(async()=>{
        try{
          const status=await readPublicGatewayStatus();
          if(attempt===connectionAttempt)renderPublicGatewayStatus(status);
        }catch(error){
          if(attempt===connectionAttempt)renderPublicGatewayStatus(null,error.message);
        }finally{
          if(attempt===connectionAttempt){
            publicGatewayLoading=false;
            renderDesktopAccess();
            const delay=publicGatewayReachable&&publicGatewayEnabled?6000:20000;
            publicGatewayTimer=setTimeout(()=>void refreshPublicGateway(),delay);
          }
        }
      })().finally(()=>{if(publicGatewayTask?.promise===promise)publicGatewayTask=undefined;});
      publicGatewayTask={attempt,promise};
      return promise;
    }
    window.addEventListener('online',()=>void refreshPublicGateway());
    document.addEventListener('visibilitychange',()=>{if(!document.hidden)void refreshPublicGateway();});
    const publicGatewayDialog=$('#claude-desktop-dialog');
    function showCreatedGatewayAccount(){
      if(!publicGatewayDialog.open||!publicGatewayCreatedAccount||publicGatewayOperation)return;
      const {username,password}=publicGatewayCreatedAccount;
      publicGatewayCreatedAccount=undefined;
      void showAppMessage('已创建 VPS 上的 CloudCLI 网页账户，请妥善保存。\n\n账号 '+username+'\n密码 '+password,
        {title:'CloudCLI 账户已创建',confirm:'已保存',clearOnClose:true});
    }
    function openPublicGatewaySettings(){
      if(publicGatewayDialog.open||$('#claude-desktop-manage').disabled)return;
      renderDesktopAccess();
      publicGatewayDialog.showModal();
      void refreshPublicGateway(true);
      showCreatedGatewayAccount();
    }
    $('#claude-desktop-manage').onclick=openPublicGatewaySettings;
    $('#public-gateway-runtime').onclick=()=>{
      openPublicGatewaySettings();
      if(!publicGatewayCreatedAccount&&!$('#app-message-dialog').open&&publicGatewayEnabled&&!publicGatewayLegacy)
        void openManagedGateway();
    };
    $('#claude-desktop-close').onclick=()=>publicGatewayDialog.close();
    publicGatewayDialog.addEventListener('close',()=>{
      if(!publicGatewayDialog.open)$('#safe-code-cloudcli-api-key').value='';
    });
    const repairDialog=$('#gateway-repair-dialog'), repairStatus=$('#gateway-repair-status');
    const repairCloud=$('#gateway-repair-cloudcli'), repairStart=$('#gateway-repair-start');
    const repairBack=$('#gateway-repair-back');
    let repairing=false;
    function setRepairBusy(value){
      repairing=value;
      repairCloud.disabled=repairStart.disabled=value;
      for(const button of repairDialog.querySelectorAll('[data-close-gateway-repair]'))button.disabled=value;
    }
    $('#safe-code-api-repair').onclick=()=>{
      if($('#safe-code-api-repair').disabled||repairDialog.open)return;
      repairCloud.checked=false;
      setRepairBusy(false);
      repairStart.hidden=false;
      repairBack.textContent='取消';
      repairStatus.textContent='重装期间服务会短暂中断，建议等正在运行的请求完成。';
      repairDialog.showModal();
    };
    for(const button of repairDialog.querySelectorAll('[data-close-gateway-repair]')){
      button.onclick=()=>{if(!repairing)repairDialog.close();};
    }
    repairDialog.addEventListener('cancel',event=>{if(repairing)event.preventDefault();});
    function showCloudCliLogin(login,{repaired=false,needsSetup=false,showIf}={}){
      let message='这台服务器没有可查询的 CloudCLI 登录信息。若账号是在网页上手动注册的，请使用当时设置的账号和密码。';
      if(needsSetup){
        message='CloudCLI 尚未创建网页账号。请打开 CloudCLI，按网页提示完成首次注册。';
      }else if(login?.state==='unverified'){
        message='保存的登录信息暂未通过 CloudCLI 验证。请检查网页服务。如果改过密码，请使用当前密码登录。';
      }else if(login?.state==='verified'&&login.username&&login.password){
        const intro=repaired?'CloudCLI 已重装，以下登录信息已通过验证。':'以下登录信息已通过 CloudCLI 验证。';
        message=intro+'\n\n用户名 '+login.username+'\n密码 '+login.password;
      }
      return showAppMessage(message,{title:'CloudCLI 登录信息',confirm:'完成',clearOnClose:true,preserveWhitespace:true,showIf});
    }
    $('#gateway-repair-form').onsubmit=async event=>{
      event.preventDefault();
      if(repairing||repairStart.hidden||!connectionReady||publicGatewayOperation)return;
      const attempt=connectionAttempt, reinstallCloudCLI=repairCloud.checked;
      setRepairBusy(true);
      setPublicGatewayOperation('正在修复网关。');
      repairStatus.textContent=reinstallCloudCLI?'正在重装网关和 CloudCLI，下载安装可能需要几分钟。':'正在重装网关并检查服务。';
      try{
        await publicGatewayTask?.promise;
        if(attempt!==connectionAttempt)return;
        const status=await native('safeCodeApi.repair',{accepted:true,reinstallCloudCLI});
        if(attempt!==connectionAttempt)return;
        publicGatewayError=undefined;
        renderPublicGatewayStatus(status);
        repairStatus.textContent=status.cloudcliReinstalled?'网关和 CloudCLI 已重装，服务检查通过。':'网关已重装，服务检查通过。';
        repairStart.hidden=true;
        repairBack.textContent='完成';
        if(reinstallCloudCLI&&status.cloudcliReinstalled&&repairDialog.open)
          void showCloudCliLogin(status.cloudcliLogin,{repaired:true,needsSetup:status.cloudcliNeedsSetup,
            showIf:()=>attempt===connectionAttempt&&connectionReady});
      }catch(error){
        if(attempt===connectionAttempt)repairStatus.textContent=error.message;
      }finally{
        setRepairBusy(false);
        if(attempt===connectionAttempt){
          setPublicGatewayOperation('');
          void refreshPublicGateway();
        }
      }
    };
    $('#safe-code-open-cloudcli').onclick=event=>{
      event.preventDefault();
      const attempt=connectionAttempt;
      if(!publicGatewayOperation)native('cloud.open').catch(error=>{
        if(attempt!==connectionAttempt)return;
        publicGatewayError={message:error.message};renderDesktopAccess();
      });
    };
    $('#safe-code-api-form').onsubmit=async event=>{
      event.preventDefault();
      const updating=event.submitter?.id==='safe-code-api-update';
      if($(updating?'#safe-code-api-update':'#safe-code-api-enable').disabled)return;
      const attempt=connectionAttempt, keyField=$('#safe-code-cloudcli-api-key');
      const apiKey=publicGatewayNeedsCloudKey?keyField.value.trim():'';
      if(publicGatewayNeedsCloudKey&&!apiKey){
        publicGatewayError={message:'请填写 CloudCLI API Key。'};renderDesktopAccess();keyField.focus();return;
      }
      publicGatewayError=undefined;
      let needsCloudKey=false;
      setPublicGatewayOperation('正在确认服务器上的网关状态。');
      try{
        await publicGatewayTask?.promise;
        if(attempt!==connectionAttempt)return;
        if(updating||!publicGatewayReachable){
          const status=await readPublicGatewayStatus();
          if(attempt!==connectionAttempt)return;
          renderPublicGatewayStatus(status);
        }
        if(updating!==publicGatewayUpgrade)return;
        if(publicGatewayMissing||publicGatewayUpgrade){
          keyField.value='';
          setPublicGatewayOperation(updating?'正在更新公网网关。':'正在准备 CloudCLI 并部署公网网关，可能需要几分钟。');
          const status=updating&&!publicGatewayLegacy?
            await native('safeCodeApi.repair',{accepted:true,reinstallCloudCLI:false}):
            await native('safeCodeApi.install',apiKey?{accepted:true,cloudcli:{apiKey}}:{accepted:true},{
            ms:13*60000,message:'等待网关部署结果超时，服务器可能仍在处理。'
          });
          if(attempt!==connectionAttempt)return;
          if((status.cloudcliKeyRequired||status.backend==='cloudcli')&&!nativeServer.cloudReady)
            applyServer({...nativeServer,cloudReady:true});
          if(status.cloudcliKeyRequired){
            publicGatewayNeedsCloudKey=needsCloudKey=true;
            return;
          }
          publicGatewayCreatedAccount=status.cloudcliAccount;
          renderPublicGatewayStatus(status);
        }
        if(updating){
          if(publicGatewayUpgrade)throw new Error('网关仍报告旧版本，请重试更新。');
          return;
        }
        if(!publicGatewayEnabled){
          setPublicGatewayOperation('正在启用公网网关。');
          const status=await native('safeCodeApi.enablePublic',{accepted:true},{
            ms:35000,message:'等待公网网关启用结果超时。'
          });
          if(attempt!==connectionAttempt)return;
          renderPublicGatewayStatus(status);
        }
        if(!publicGatewayEnabled)publicGatewayError={action:'enable',message:'未能确认公网已启用，点击「启用」重试。'};
      }catch(error){
        if(attempt===connectionAttempt){
          publicGatewayReachable=false;
          publicGatewayError={action:updating?'update':'enable',message:error.message+(updating?' 检查连接后重试更新。':' 检查连接后，点击「启用」重试。')};
        }
      }finally{
        if(attempt===connectionAttempt){
          setPublicGatewayOperation('');
          if(needsCloudKey&&publicGatewayDialog.open)keyField.focus();
          showCreatedGatewayAccount();
          void refreshPublicGateway();
        }
      }
    };
    async function openManagedGateway(){
      if($('#safe-code-api-manage').disabled)return;
      const attempt=connectionAttempt;
      publicGatewayError=undefined;
      setPublicGatewayOperation('正在打开网关管理。');
      try{
        await native('window.gateway.open',{},{
          ms:35000,message:'打开网关管理超时。'
        });
        if(attempt===connectionAttempt)publicGatewayDialog.close();
      }catch(error){
        if(attempt===connectionAttempt)publicGatewayError={action:'manage',message:error.message};
      }finally{
        if(attempt===connectionAttempt){
          setPublicGatewayOperation('');
          void refreshPublicGateway();
        }
      }
    }
    $('#safe-code-api-manage').onclick=openManagedGateway;
    async function openLocalClient(button,operation,onSettled,progress){
      if(button.disabled)return;openingClients.add(button);button.disabled=true;
      try{
        const result=await native(operation);
        if(result?.installationRequired)await showAppMessage(result.message);
        else if(progress)progress.textContent=result;
      }
      catch(error){if(progress)progress.textContent=error.message;else showAppMessage(error.message);}
      finally{openingClients.delete(button);onSettled();}
    }
    const keyDialog=$('#desktop-key-dialog'), keyInput=$('#desktop-key-input'), keyProgress=$('#desktop-key-progress');
    const keyEnable=$('#desktop-key-enable'), keyReplace=$('#desktop-key-replace'), keyResume=$('#desktop-key-resume');
    let keyReplacing=false, keyDeviceSetup=null;
    const apiScreen=$('#api-screen'), apiUsage=$('#api-usage-message'), apiTable=$('#api-model-table');
    let apiModeWrite=Promise.resolve();
    function rememberApiMode(enabled){
      apiModeWrite=apiModeWrite.then(()=>native('desktopKey.mode',{enabled})).catch(()=>{});
    }
    let apiReturn='login', apiRequest=0, apiFetching=false;
    const usd=new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:4});
    const tokens=new Intl.NumberFormat('zh-CN');
    function money(value){
      return value>0&&value<100000?'< $0.0001':usd.format(value/1e9);
    }
    function renderApiDuration(){
      const start=keyState.startedAt;
      if(!keyState.running||!Number.isFinite(start)){ $('#api-duration').textContent='—';return; }
      const elapsed=Math.max(0,Math.floor((Date.now()-start)/1000));
      const hours=Math.floor(elapsed/3600), minutes=Math.floor(elapsed/60)%60, seconds=elapsed%60;
      $('#api-duration').textContent=hours?`${hours} 小时 ${minutes} 分`:minutes?`${minutes} 分 ${seconds} 秒`:`${seconds} 秒`;
    }
    function renderApi(){
      $('#api-state-label').textContent=connectionLabel();
      $('#api-state').classList.toggle('connected',!!(keyState.running&&keyState.gatewayConnected));
      $('#api-device').textContent=keyState.deviceAlias||'';
      $('#api-manage-key').textContent=keyState.running?'接入设置':'恢复接入';
      setLaunchAvailability('api-open-claude',!!keyState.running&&!keyBusy);
      $('#api-usage-refresh').disabled=!keyState.running||apiFetching;
      renderApiDuration();
    }
    function resetApiUsage(message){
      apiRequest++;apiFetching=false;
      $('#api-balance').textContent=$('#api-spend').textContent='—';
      $('#api-models').replaceChildren();apiTable.hidden=true;
      apiUsage.hidden=false;apiUsage.textContent=message;
    }
    async function refreshApiUsage(){
      if(apiScreen.hidden||!keyState.running||apiFetching)return;
      apiFetching=true;renderApi();
      const request=++apiRequest;
      try{
        const data=await native('desktopKey.balance');
        if(request!==apiRequest)return;
        $('#api-balance').textContent=data.remainingNanos===null?'不限额':money(data.remainingNanos);
        $('#api-spend').textContent=money(data.usedNanos);
        const rows=Array.isArray(data.models)?data.models:[];
        const elements=rows.map(model=>{
          const row=document.createElement('tr');
          const count=model.input+model.output+model.cacheRead+model.cacheWrite;
          const fee=model.unpricedTokens>0?(model.costNanos?money(model.costNanos)+' + 未计价':'未计价'):money(model.costNanos);
          for(const [index,value] of [model.model,tokens.format(count),fee].entries()){
            const cell=document.createElement(index===0?'th':'td');
            if(index===0)cell.scope='row';
            cell.textContent=value;row.append(cell);
          }
          return row;
        });
        $('#api-models').replaceChildren(...elements);
        apiTable.hidden=!rows.length;apiUsage.hidden=!!rows.length;
        apiUsage.textContent=Array.isArray(data.models)?'当前 Key 暂无模型用量。':'当前网关版本暂不提供模型明细。';
      }catch{
        if(request!==apiRequest)return;
        apiUsage.hidden=false;
        apiUsage.textContent=apiTable.hidden?'暂时无法获取用量，请点击「刷新用量」重试。':'刷新失败，已保留上次查询的数据。';
      }finally{
        if(request===apiRequest){apiFetching=false;renderApi();}
      }
    }
    function showApiAccess(remember=true){
      if(apiScreen.hidden){
        apiReturn=$('#workspace-screen').hidden?'login':'workspace';
        if(remember)rememberApiMode(true);
        showScreen('api');$('#api-title').focus();
      }
      if(keyDialog.open)keyDialog.close();
      resetApiUsage(keyState.running?'正在查询用量…':'恢复接入后即可查看用量。');
      renderApi();void refreshApiUsage();
    }
    function closeApiAccess(){
      rememberApiMode(false);
      resetApiUsage('正在查询用量…');
      showScreen(apiReturn==='workspace'&&connectionReady?'workspace':'login');
    }
    $('#api-server-mode').onclick=closeApiAccess;
    $('#api-usage-refresh').onclick=refreshApiUsage;
    $('#api-open-claude').onclick=()=>openLocalClient($('#api-open-claude'),'claudeDesktop.open',renderApi);
    const keyListDialog=$('#key-list-dialog'), keyList=$('#key-list'), keyForm=$('#key-add-form');
    const keyMessage=$('#key-list-status');
    let libraryBusy=false, switchPending=false, listGeneration=0;
    let editingKeyId=null;
    function editKeyForm(entry){
      editingKeyId=entry?.id??null;
      keyForm.reset();
      keyForm.hidden=false;
      const code=$('#key-code'), label=$('#key-label');
      code.hidden=code.labels[0].hidden=!!entry;
      code.required=!entry;
      label.labels[0].textContent=entry?'编辑备注':'名称';
      label.value=entry?.name??'';
      keyForm.querySelector('[type=submit]').textContent=entry?'保存备注':'保存 Key';
      keyMessage.textContent='';
      label.focus();label.select();
    }
    function lockLibrary(busy){
      libraryBusy=busy;
      for(const field of keyListDialog.querySelectorAll('button,input'))field.disabled=busy;
    }
    async function loadKeyList(){
      const generation=++listGeneration;
      const rows=await native('desktopKey.list');
      if(!keyListDialog.open||generation!==listGeneration)return;
      keyList.replaceChildren();
      if(!rows.length)keyMessage.textContent='尚未保存 Key。';
      for(const entry of rows){
        const row=document.createElement('div'), info=document.createElement('div');
        row.className='key-row'+(entry.running?' current':'');
        const name=document.createElement('strong'), balance=document.createElement('small');
        const heading=document.createElement('div'), edit=document.createElement('button');
        heading.className='key-row-name';edit.className='text-button';edit.type='button';
        name.textContent=entry.name;edit.textContent='编辑备注';
        edit.setAttribute('aria-label','编辑备注：'+entry.name);
        edit.onclick=()=>editKeyForm(entry);
        heading.append(name,edit);balance.textContent='余额查询中…';info.append(heading,balance);
        const action=document.createElement(entry.running?'span':'button');
        action.className=entry.running?'key-current':'key-secondary';
        action.textContent=entry.running?'正在使用':'切换';
        if(!entry.running){
          action.type='button';action.setAttribute('aria-label','切换到 '+entry.name);
          action.onclick=()=>switchSavedKey(entry);
        }
        const actions=document.createElement('div');actions.className='key-row-actions';
        if(!entry.current){
          const forget=document.createElement('button');
          forget.type='button';forget.className='text-button';forget.textContent='移除';
          forget.setAttribute('aria-label','移除 '+entry.name);
          forget.onclick=async()=>{
            if(libraryBusy||!await showAppMessage('从本机移除「'+entry.name+'」？',{confirm:'移除',cancel:'取消'}))return;
            lockLibrary(true);
            try{await native('desktopKey.forget',{id:entry.id});await loadKeyList();}
            catch(error){keyMessage.textContent=error.message;}
            finally{lockLibrary(false);}
          };
          actions.append(forget);
        }
        actions.append(action);row.append(info,actions);keyList.append(row);
        native('desktopKey.savedBalance',{id:entry.id}).then(data=>{
          balance.textContent=data.remainingNanos===null?'不限额':'余额 '+money(data.remainingNanos);
        }).catch(()=>{balance.textContent='余额暂不可用';});
      }
    }
    async function openKeyList(){
      if(keyBusy||libraryBusy)return;
      keyForm.hidden=true;keyForm.reset();keyMessage.textContent='';keyList.replaceChildren();
      keyListDialog.showModal();keyListDialog.scrollTop=0;
      try{await loadKeyList();}catch(error){keyMessage.textContent=error.message;}
    }
    async function finishKeySwitch(){
      switchPending=false;keyListDialog.close();showApiAccess();
      try{const result=await native('claudeDesktop.reopen');if(result?.installationRequired)await showAppMessage(result.message);}
      catch(error){await showAppMessage('已切换 Key。'+error.message);}
    }
    async function refreshSwitchState(){
      keyState=await native('desktopKey.status');
      if(!keyState.running)resetApiUsage('选择已保存的 Key 恢复接入。');
      renderKey();
      await loadKeyList();
    }
    async function switchSavedKey(entry){
      if(libraryBusy)return;
      lockLibrary(true);keyMessage.textContent='正在连接 '+entry.name+'…';
      try{
        const result=await native('desktopKey.enable',{savedId:entry.id,replace:true});
        if(result.requiresDevice){
          switchPending=true;keyDeviceSetup=result;openHostControl();return;
        }
        keyState=result;
        if(!keyState.running)throw new Error(keyState.error||'接入未启动。');
        await finishKeySwitch();
      }catch(error){
        keyMessage.textContent=error.message;
        try{await refreshSwitchState();}catch{}
      }finally{lockLibrary(false);}
    }
    $('#api-switch-key').onclick=openKeyList;
    $('#key-library-entry').onclick=openKeyList;
    $('#key-list-close').onclick=()=>keyListDialog.close();
    keyListDialog.addEventListener('cancel',event=>{if(libraryBusy)event.preventDefault();});
    keyListDialog.addEventListener('close',()=>{keyForm.reset();listGeneration++;});
    $('#key-add').onclick=()=>editKeyForm(null);
    $('#key-add-cancel').onclick=()=>{keyForm.hidden=true;keyForm.reset();};
    keyForm.onsubmit=async event=>{
      event.preventDefault();if(libraryBusy)return;
      lockLibrary(true);
      try{
        const name=$('#key-label').value.trim();
        if(editingKeyId)await native('desktopKey.rename',{id:editingKeyId,name});
        else await native('desktopKey.save',{name,key:$('#key-code').value.trim()});
        keyForm.reset();keyForm.hidden=true;keyMessage.textContent='';
        await loadKeyList();
      }catch(error){keyMessage.textContent=error.message;}
      finally{lockLibrary(false);}
    };
    $('#api-manage-key').onclick=()=>{
      keyDialog.showModal();void keyAction('status',{interactive:true});
    };
    setInterval(()=>{if(!apiScreen.hidden)renderApiDuration();},1000);
    setInterval(refreshApiUsage,30000);
    let readingConnection=false;
    async function refreshDesktopConnection(){
      if(document.hidden||!keyState.running||keyBusy||readingConnection)return;
      const start=keyState.startedAt;
      readingConnection=true;
      try{
        const state=await native('desktopKey.connection');
        if(state && keyState.running && !keyBusy && start===keyState.startedAt && state.startedAt===start){
          Object.assign(keyState,state);
          renderApi();renderDesktopAccess();
        }
      }catch{}finally{readingConnection=false;}
    }
    setInterval(refreshDesktopConnection,3000);
    document.addEventListener('visibilitychange',refreshDesktopConnection);
    function describeKey(op,args){
      const target=keyState.deviceAlias?'\n工具目标：已授权的本机设备 '+keyState.deviceAlias:'';
      if(keyState.unbindPending)return keyState.error;
      if(keyState.error)return (keyState.configured?'已保存 Key，暂未恢复接入。\n':'')+keyState.error;
      if(keyState.running)return connectionLabel()+'。'+target+'\n'+(args?.replace||args?.useSaved?'请完全退出并重新打开 Claude Desktop 以应用更改。':'首次接入请完全退出并重新打开 Claude Desktop。');
      if(keyState.configured)return '已保存 Key，当前 Claude 配置未启用此接入。';
      return op==='disable'?'已停用。\n请重新打开 Claude Desktop 以应用更改。':'尚未启用。';
    }
    function renderKey(){
      const configured=keyState.configured;
      const editing=!configured||keyReplacing;
      $('#desktop-key-field').hidden=!editing;
      keyInput.disabled=keyBusy||!editing;
      keyEnable.hidden=!editing;
      keyEnable.disabled=keyBusy;
      keyEnable.textContent=keyReplacing?'验证并更换':'验证并启用';
      keyReplace.hidden=!configured||keyState.unbindPending;
      keyReplace.disabled=keyBusy;
      keyReplace.textContent=keyReplacing?'取消更换':'更换 Key';
      keyResume.hidden=!configured||keyState.running||keyReplacing||keyState.unbindPending;
      keyResume.disabled=keyBusy;
      $('#desktop-key-refresh').hidden=!keyState.running||keyReplacing;
      $('#desktop-key-refresh').disabled=keyBusy;
      $('#desktop-key-disable').hidden=!configured||keyReplacing;
      $('#desktop-key-disable').disabled=keyBusy;
      $('#desktop-key-disable').textContent=keyState.unbindPending?'重试解绑':'停用接入';
      $('#desktop-key-open').hidden=!keyState.running||keyReplacing;
      setLaunchAvailability('desktop-key-open',keyState.running&&!keyBusy&&!keyReplacing);
      $('#desktop-key-close').disabled=keyBusy;
      renderDesktopAccess();
      if(!apiScreen.hidden)renderApi();
    }
    async function keyAction(op,args={}){
      if(keyBusy)return;
      keyBusy=true;
      if(op==='enable')keyProgress.textContent=args.useSaved?'正在重新启用已保存的 Key…':'正在验证 Key 并获取模型…';
      else if(op==='refreshModels')keyProgress.textContent='正在更新模型列表…';
      else keyProgress.textContent=op==='disable'?'正在停用接入…':'正在读取接入状态…';
      renderKey();
      try{
        const result=await native('desktopKey.'+op,args);
        if(result?.requiresDevice){
          keyProgress.textContent='网关已验证，请确认本机远程控制。';
          keyDeviceSetup=result;
          openHostControl();
          return;
        }
        keyState=result;
        if(op==='enable'&&keyState.running)keyReplacing=false;
        keyProgress.textContent=op==='refreshModels'?'模型列表已更新。请完全退出并重新打开 Claude Desktop。':describeKey(op,args);
        if(op==='enable'&&keyState.running)showApiAccess();
        if(op==='disable'&&!keyState.unbindPending&&!apiScreen.hidden){keyDialog.close();closeApiAccess();}
      }catch(error){
        keyProgress.textContent=error.message;
      }finally{keyBusy=false;renderKey();}
    }
    for(const id of ['desktop-key-login','desktop-key-settings','desktop-key-workspace']){
      const button=$('#'+id);button.hidden=false;
      button.onclick=()=>{
        if(keyDialog.open)return;
        if(keyState.running){showApiAccess();return;}
        keyDialog.showModal();
        keyAction('status',{interactive:true});
      };
    }
    $('#desktop-key-form').onsubmit=event=>{
      event.preventDefault();
      const key=keyInput.value.trim();
      if(!keyBusy&&!keyInput.disabled&&key){
        keyInput.value='';
        keyAction('enable',keyReplacing?{key,replace:true}:{key});
      }
    };
    keyReplace.onclick=()=>{
      if(keyBusy)return;
      keyReplacing=!keyReplacing;
      keyInput.value='';
      keyProgress.textContent=keyReplacing?'粘贴新的接入 Key，验证通过后切换。':describeKey();
      renderKey();
      if(keyReplacing)keyInput.focus();
    };
    keyResume.onclick=()=>keyAction('enable',{useSaved:true});
    $('#desktop-key-refresh').onclick=()=>keyAction('refreshModels');
    $('#desktop-key-disable').onclick=()=>keyAction('disable');
    $('#desktop-key-open').onclick=()=>openLocalClient($('#desktop-key-open'),'claudeDesktop.open',renderKey,keyProgress);
    $('#desktop-key-close').onclick=()=>keyDialog.close();
    keyDialog.addEventListener('cancel',event=>{if(keyBusy)event.preventDefault();});
    keyDialog.addEventListener('close',()=>{keyInput.value='';keyReplacing=false;});
    $('#codex-launch').onclick=()=>openLocalClient($('#codex-launch'),'codex.open',renderClaudeBridge);
    const hostDialog=$('#host-dialog');
    const hostVerified=status=>status?.enabled===true&&status.state==='connected';
    const isHostSetupCurrent=attempt=>attempt===connectionAttempt&&connectionReady&&!!nativeServer?.statuses?.account;
    function renderKeyDeviceSetup(){
      $('#host-dialog-state').textContent='等待授权';
      $('#host-close').disabled=hostBusy;
      $('#host-risk').disabled=hostBusy;
      $('#host-alias').disabled=true;
      $('#host-enable').hidden=false;
      $('#host-enable').disabled=hostBusy||!$('#host-risk').checked;
      $('#host-enable').textContent=keyDeviceSetup.existingControl
        ? (hostBusy?'正在验证…':'同意并使用')
        : (hostBusy?'正在部署…':'同意并开始部署');
      $('#host-disable').hidden=true;
      $('#host-command-area').hidden=true;
    }
    function renderHostControl(value){
      if(keyDeviceSetup){renderKeyDeviceSetup();return;}
      if(value)hostStatus=value;
      const enabled=!!hostStatus?.enabled,connected=hostVerified(hostStatus);
      $('#host-state').textContent=enabled?(connected?'已连接':hostStatus?.state==='attention'?'需要处理':'重连中'):'未启用';
      $('#host-summary').textContent=enabled?(hostStatus.message||'本机控制已启用'):'让服务器上的 AI 连接本机命令行，操作本机文件和程序。';
      $('#host-manage').disabled=hostBusy||busy||!nativeServer?.statuses?.account;
      if(enabled)$('#host-risk').checked=true;
      $('#host-close').disabled=hostBusy;$('#host-alias').disabled=hostBusy||enabled;$('#host-risk').disabled=hostBusy||enabled;
      $('#host-enable').hidden=enabled;$('#host-enable').disabled=hostBusy||!$('#host-risk').checked;$('#host-enable').textContent=hostBusy?'正在部署…':'同意并开始部署';
      $('#host-disable').hidden=!enabled;$('#host-disable').disabled=hostBusy;
      $('#host-command-area').hidden=!enabled;$('#host-command').value=hostStatus?.command||'';$('#host-copy').disabled=!connected||!hostStatus?.command||hostBusy;
      $('#host-dialog-state').textContent=$('#host-state').textContent;
    }
    function resetHostControl(){
      hostChange++;
      hostStatus=null;
      if(hostDialog.open)hostDialog.close();
      renderHostControl();
    }
    async function readHostControl(attempt){
      if(hostBusy||!isHostSetupCurrent(attempt))return null;
      const change=hostChange;
      if(hostStatusTask?.attempt!==attempt||hostStatusTask?.change!==change){
        hostStatusTask={attempt,change,promise:native('host.status')};
      }
      const task=hostStatusTask;
      try{
        const status=await task.promise;
        if(hostBusy||change!==hostChange||!isHostSetupCurrent(attempt))return null;
        if(typeof status?.enabled!=='boolean')throw new Error('无法确认本机远程控制状态，请重新检查服务器连接。');
        renderHostControl(status);
        return status;
      }finally{if(hostStatusTask===task)hostStatusTask=null;}
    }
    function openHostControl(){
      $('#host-risk').checked=false;
      $('#host-alias').value=keyDeviceSetup
        ? (keyDeviceSetup.alias??'由网关分配')
        : (hostStatus?.alias||(isMac?'mac':'win'));
      const instructions=hostStatus?.enabled?hostStatus.message:(isMac
        ?'请先在系统设置 → 通用 → 共享开启远程登录，仅允许当前账户。Safe Code 将添加专用密钥，不会开启或更改系统 SSH 服务。'
        :'启用时会请求 Windows 管理员授权；首次部署需要几秒至几十秒。');
      $('#host-progress').textContent=keyDeviceSetup
        ? (keyDeviceSetup.reuseControl
          ? (keyDeviceSetup.existingControl
            ? '这把 Key 由当前 VPS 签发。授权后将使用已验证的本机远程控制通道。'
            : '这把 Key 由当前 VPS 签发。授权后将通过当前 SSH 会话开启本机远程控制。')
          : (isMac
            ? '将在 Key 所属的网关建立加密通道，并为此设备分配唯一连接别名。SafeCode 只会为当前 macOS 账户添加一把专用 SSH 公钥。'
            : '将在 Key 所属的网关建立加密通道，并为此设备分配唯一连接别名。部署需要 Windows 管理员授权。'))
        : instructions;
      if(keyDeviceSetup)$('#host-progress').textContent+=' 下次在这台电脑选择同一把 Key 时，会使用本次授权重新连接。';
      renderHostControl();
      hostDialog.showModal();
    }
    async function pollHostControl(){
      if(keyDeviceSetup)return;
      try{await readHostControl(connectionAttempt);}catch{}
    }
    $('#host-manage').onclick=async()=>{
      const attempt=connectionAttempt;
      try{
        await readHostControl(attempt);
        if(isHostSetupCurrent(attempt)&&!hostDialog.open&&!hostBusy)openHostControl();
      }catch(error){if(attempt===connectionAttempt)await showAppMessage(error.message);}
    };
    $('#host-close').onclick=()=>{if(!hostBusy)hostDialog.close();};
    hostDialog.addEventListener('cancel',event=>{if(hostBusy)event.preventDefault();});
    hostDialog.addEventListener('close',()=>{
      if(hostDialog.open)return;
      if(keyDeviceSetup){
        keyDeviceSetup=null;
        void native('desktopKey.cancelDevice').catch(()=>{});
        if(switchPending){switchPending=false;keyMessage.textContent='已取消切换。';}
        else keyDialog.close();
        renderHostControl();
      }
    });
    $('#host-risk').onchange=()=>renderHostControl();
    $('#host-form').onsubmit=async event=>{
      event.preventDefault();if(hostBusy||!$('#host-risk').checked||!$('#host-form').reportValidity())return;
      if(keyDeviceSetup){
        hostBusy=true;renderHostControl();
        $('#host-progress').textContent=keyDeviceSetup.reuseControl
          ? (keyDeviceSetup.existingControl
            ? '正在检查已授权的本机控制通道并启用 Key…'
            : (isMac?'正在为系统远程登录添加专用公钥，接下来会验证本机远程控制并启用 Key。':'请完成 Windows 管理员授权。接下来会验证本机远程控制并启用 Key。'))
          : (isMac?'正在为系统远程登录添加专用公钥，并建立 FRP 通道验证 SSH 登录。':'请完成 Windows 管理员授权。接下来会建立 FRP 通道并验证 SSH 登录。');
        try{
          keyState=await native('desktopKey.authorizeDevice',{riskAccepted:true});
          keyDeviceSetup=null;keyReplacing=false;
          keyProgress.textContent=describeKey('enable');
          hostDialog.close();renderKey();
          if(keyState.running){if(switchPending)await finishKeySwitch();else showApiAccess();}
        }catch(error){
          $('#host-risk').checked=false;
          $('#host-progress').textContent=error.message;
          if(switchPending)try{await refreshSwitchState();}catch{}
        }finally{hostBusy=false;renderHostControl();}
        return;
      }
      const attempt=connectionAttempt;
      if(!isHostSetupCurrent(attempt))return;
      hostBusy=true;hostChange++;renderHostControl();$('#host-progress').textContent=isMac?'正在为系统远程登录添加专用公钥，并验证 macOS 账户身份…':'正在准备专用密钥与本机 SSH，请完成 Windows 管理员授权。随后会验证服务器能否登录本机…';
      try{
        const status=await native('host.enable',{alias:$('#host-alias').value.trim(),riskAccepted:true});
        if(!isHostSetupCurrent(attempt))return;
        renderHostControl(status);
        $('#host-progress').textContent=hostVerified(status)?'部署完成，已通过实际 SSH 登录验证。复制下方命令发给服务器上的 AI。':(status?.message||'正在验证本机远程控制。');
      }
      catch(error){if(attempt===connectionAttempt){$('#host-risk').checked=false;$('#host-progress').textContent=error.message;}}
      finally{hostBusy=false;renderHostControl();}
    };
    $('#host-disable').onclick=async()=>{
      if(hostBusy)return;const attempt=connectionAttempt;
      hostBusy=true;hostChange++;renderHostControl();$('#host-progress').textContent=isMac?'正在断开专用通道并撤销密钥；系统远程登录设置保持不变…':'正在关闭控制通道并停止本机 SSH…';
      try{
        const status=await native('host.disable');
        if(attempt!==connectionAttempt)return;
        renderHostControl(status);$('#host-risk').checked=false;$('#host-progress').textContent='本机远程控制已停用。';
      }
      catch(error){if(attempt===connectionAttempt)$('#host-progress').textContent=error.message;}
      finally{hostBusy=false;renderHostControl();}
    };
    $('#host-copy').onclick=async()=>{if(!hostStatus?.command)return;try{await native('copy',{text:hostStatus.command});$('#host-progress').textContent='命令已复制。请发给当前服务器上的 AI。';}catch(error){$('#host-progress').textContent=error.message;}};
    setInterval(()=>{if(!$('#workspace-screen').hidden)pollHostControl();},2000);
    let ipSaving=false;
    const ipProgress=document.createElement('div');ipProgress.className='connection-progress';ipProgress.hidden=true;ipProgress.setAttribute('role','status');ipProgress.setAttribute('aria-live','polite');
    const ipSpinner=document.createElement('span');ipSpinner.className='connection-spinner';ipSpinner.setAttribute('aria-hidden','true');const ipProgressText=document.createElement('span'),ipElapsed=document.createElement('span');ipProgress.append(ipSpinner,ipProgressText,ipElapsed);$('#ip-policy-form .config-footer').before(ipProgress);
    $('#ip-policy-dialog').addEventListener('cancel',event=>{if(ipSaving)event.preventDefault();});
    const originalOpenIpPolicy=openIpPolicy;openIpPolicy=function(){
      if(busy){showAppMessage('已有任务正在执行，请等待完成。');return;}
      if(nativeServer?.socksEgress?.enabled){showAppMessage('请先在「开发模式」停用 SOCKS5 出口，再更换 IP。');return;}
      originalOpenIpPolicy();
      $('#ingress-ip-input').value=ipPolicy.ingressIp||serverConnection.ingressIp;
      const candidates=(nativeServer?.assignedIps||[]).filter(isPublicIpv4);
      $('#egress-ip-input').value=ipPolicy.egressIp||(candidates.length===2?candidates.find(ip=>ip!==serverConnection.ingressIp):serverConnection.egressIp)||'';
      syncIpPickers();$('#ip-policy-hint').textContent='只使用服务器已绑定的 IP。保存时自动检查入口和真实出口；无法验证会恢复原设置。';
    };
    $('#ip-policy-form').onsubmit=async e=>{
      e.preventDefault();if(ipSaving)return;syncIpPickers();closeIpPickers();if(busy){showAppMessage('已有任务正在执行，请等待完成。');return;}if(!$('#ip-policy-form').reportValidity())return;
      const args={ingressIp:$('#ingress-ip-input').value.trim(),egressIp:$('#egress-ip-input').value.trim(),lockIngress:$('#lock-ingress').checked,lockEgress:$('#lock-egress').checked};
      for(const [key,id] of [['ingressIp','ingress-ip-input'],['egressIp','egress-ip-input']]){
        if(!isPublicIpv4(args[key])||!nativeServer?.assignedIps?.includes(args[key])){$('#ip-policy-hint').textContent='未检测到有效的已绑定 IP，请重新连接服务器后再选择。';return;}
      }
      ipSaving=true;busy=true;const form=$('#ip-policy-form'),button=form.querySelector('[type=submit]');const controls=[...form.querySelectorAll('button,input,select'),$('#close-ip-policy')].map(control=>[control,control.disabled]);
      for(const [control] of controls)control.disabled=true;form.setAttribute('aria-busy','true');button.textContent='正在验证…';ipProgress.hidden=false;ipProgressText.textContent='正在检查 IP 和路由…';ipElapsed.textContent='已用时 0 秒';
      const started=Date.now();const timer=setInterval(()=>{ipElapsed.textContent='已用时 '+Math.floor((Date.now()-started)/1000)+' 秒';},1000);renderTasks();
      try{
        const result=await native('ip.apply',args);
        const ingressIp=result.ipPolicy?.ingressIp||args.ingressIp;
        if(ingressIp!==serverConnection.ingressIp){
          const [saved,draft]=await Promise.all([native('deployment.settings.get').catch(()=>null),native('account.draft.get').catch(()=>null)]);
          applyDeploymentSettings(saved);accountOptions=draft||{};accountDraftPending=!!draft;
        }
        applyServer(result);serverConnection={ingressIp,egressIp:result.observedEgressIp||''};$('#server-ingress').textContent=serverConnection.ingressIp;$('#server-address').textContent=serverConnection.egressIp||'待确认';
        $('#ip-policy-dialog').close();void queryIpInfo(serverConnection.egressIp);
      }catch(error){$('#ip-policy-hint').textContent='设置未完成，填写内容已保留。请查看错误，确认当前状态后重试。';showAppMessage(error.message);}
      finally{clearInterval(timer);ipSaving=false;busy=false;ipProgress.hidden=true;form.removeAttribute('aria-busy');for(const [control,disabled] of controls)control.disabled=disabled;button.textContent='保存设置';renderTasks();}
    };
    const originalNativeLog=window.safeCodeNativeLog;window.safeCodeNativeLog=function(step,line){if(ipSaving&&step==='ip_policy')ipProgressText.textContent=line;originalNativeLog?.(step,line);};

    const socksDialog=$('#socks-dialog'), socksUrl=$('#socks-url'), socksConfirm=$('#socks-confirm');
    let socksBusy=false, socksReading=false, socksResult=null;
    function updateSocksCard(status){
      if(!status?.enabled)return;
      $('#ip-policy-state').textContent=status.guardActive?'SOCKS5 开发模式':'出口规则异常';
      $('#ip-policy-summary').textContent=status.guardActive
        ?'机房入口保留，SOCKS5 出站。'+(status.tunnelRunning?'':'隧道暂停，出站已阻断。')
        :'SOCKS5 防火墙规则未生效，请检查服务器。';
    }
    function setSocksResult(id,value,state=''){
      const element=$('#socks-'+id);
      element.textContent=value;element.dataset.state=state;
    }
    function renderSocks(){
      const status=nativeServer?.socksEgress;
      const enabled=!!status?.enabled, available=connectionReady&&!developerPreview&&!socksBusy&&!socksReading;
      const validated=!!(socksResult?.tcp&&socksResult?.udp);
      $('#socks-setup').hidden=enabled;
      socksUrl.disabled=!available||enabled;
      $('#socks-test').disabled=!available||!socksUrl.value.trim();
      socksConfirm.disabled=!available||!validated;
      $('#socks-enable').hidden=enabled;
      $('#socks-enable').disabled=!available||!validated||!socksConfirm.checked;
      $('#socks-disable').hidden=!enabled;
      $('#socks-disable').disabled=!available;
      $('#socks-close').disabled=socksBusy;
      $('#socks-form').setAttribute('aria-busy',String(socksBusy||socksReading));
      for(const protocol of ['tcp','udp']){
        const passed=socksResult?.[protocol];
        setSocksResult(protocol,enabled?(status.observedIp?'上次验证通过':'待验证'):passed===undefined?'待检测':passed?'通过':'未通过',
          !enabled&&passed!==undefined?(passed?'pass':'fail'):'');
      }
      setSocksResult('observed',(enabled?status.observedIp:socksResult?.observedIp)||'待检测');
    }
    function socksStatusText(){
      if(!connectionReady||developerPreview)return '连接 Ubuntu 服务器后可检测和启用。';
      const status=nativeServer?.socksEgress;
      if(!status?.enabled)return '检测通过后可以启用。首次启用会下载隧道组件。';
      if(!status.guardActive)return '阻断规则未生效，请检查服务器。';
      return status.endpoint+' · '+(status.issue||(status.tunnelRunning?'已启用，代理不可用时阻断出站。':'隧道未运行，出站已阻断。'));
    }
    $('#socks-experimental').onclick=async()=>{
      if(busy){showAppMessage('已有任务正在执行，请等待完成。');return;}
      socksResult=null;
      socksConfirm.checked=false;
      socksUrl.value='';
      const server=nativeServer;
      socksReading=!!(connectionReady&&server&&!developerPreview);
      $('#socks-status').textContent=socksReading?'正在读取服务器出口状态…':socksStatusText();
      renderSocks();socksDialog.showModal();
      if(!socksReading)return;
      try{
        const status=await native('socks.status');
        if(server!==nativeServer)return;
        server.socksEgress=status;
        if(status?.enabled)updateSocksCard(status);
        $('#socks-status').textContent=socksStatusText();
      }catch(error){$('#socks-status').textContent=error.message;}
      finally{socksReading=false;renderSocks();}
    };
    socksDialog.addEventListener('cancel',event=>{if(socksBusy)event.preventDefault();});
    socksDialog.addEventListener('close',()=>{socksUrl.value='';socksConfirm.checked=false;socksResult=null;});
    socksUrl.addEventListener('input',()=>{
      socksResult=null;socksConfirm.checked=false;
      $('#socks-status').textContent=socksStatusText();renderSocks();
    });
    socksConfirm.addEventListener('change',renderSocks);
    async function runSocksOperation(message, action){
      if(busy||!connectionReady||developerPreview||socksBusy||socksReading)return;
      busy=socksBusy=true;$('#socks-status').textContent=message;
      renderSocks();renderTasks();
      try{await action();}
      catch(error){$('#socks-status').textContent=error.message;}
      finally{busy=socksBusy=false;renderSocks();renderTasks();}
    }
    $('#socks-test').onclick=()=>runSocksOperation('正在从服务器检测 TCP 和 UDP 转发…',async()=>{
      socksResult=null;socksConfirm.checked=false;
      socksResult=await native('socks.probe',{proxyUrl:socksUrl.value.trim()});
      const failures=[socksResult.tcpMessage&&'TCP '+socksResult.tcpMessage,socksResult.udpMessage&&'UDP '+socksResult.udpMessage].filter(Boolean);
      $('#socks-status').textContent=failures.join(' ')||'检测通过，可以启用。';
    });
    async function changeSocks(op,args){
      const server=await native(op,args);
      socksUrl.value='';socksConfirm.checked=false;socksResult=null;
      applyServer(server);
      serverConnection.egressIp=server.observedEgressIp||'';
      $('#server-address').textContent=serverConnection.egressIp||'待确认';
      $('#socks-status').textContent=socksStatusText();
      if(serverConnection.egressIp)void queryIpInfo(serverConnection.egressIp);
    }
    $('#socks-form').onsubmit=event=>{
      event.preventDefault();
      if(!socksResult?.tcp||!socksResult?.udp||!socksConfirm.checked)return;
      return runSocksOperation('正在部署隧道并验证整机出站…',()=>changeSocks('socks.enable',{proxyUrl:socksUrl.value.trim(),accepted:true}));
    };
    $('#socks-disable').onclick=()=>runSocksOperation('正在停用代理并恢复原出口…',()=>changeSocks('socks.disable',{}));
    // 验收入口仅在桌面启动参数指定了私密配置文件时由宿主调用。
    window.safeCodeRunAcceptance=async config=>{
      try{
        await native('acceptance.capture',{tag:'notice'});if($('#accept').getBoundingClientRect().bottom>innerHeight||$('#exit').getBoundingClientRect().bottom>innerHeight)throw new Error('声明按钮超出窗口。');$('#username').value=config.username;$('#ip').value=config.host;originalShowScreen('login');await native('window.resize',{wide:false});await native('acceptance.capture',{tag:'login'});const connectBounds=$('#connect').getBoundingClientRect();const lowest=Math.max(connectBounds.bottom,advanced.getBoundingClientRect().bottom,$('#back').getBoundingClientRect().bottom);const loginLayout={buttonBottom:connectBounds.bottom,controlsBottom:lowest,viewportHeight:innerHeight,visible:lowest<=innerHeight};if(!loginLayout.visible)throw new Error('登录连接按钮超出窗口。');const restored=await native('login.load');const restoredLogin=restored?.host===config.host&&restored?.username===config.username&&restored?.password===config.password;let result=await connect(config);config.password='';
        if(!deploymentSettingsReady())throw new Error('部署验收需要先为该连接手动保存工作用户和本土化设置。');
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
    $('#accept').disabled=true;
    preferencesReady.finally(()=>{$('#accept').disabled=false;});
    const confirmServices=()=>native('services.confirm');
    const previousAccept=$('#accept').onclick;
    $('#accept').onclick=async()=>{
      try{
        await consentChanges;
        await native('notice.accept');
        previousAccept();
        confirmServices().then(showPreferences).catch(()=>{});
      }catch{showAppMessage('无法保存声明确认，请检查本机存储空间。');}
    };
    const startupReady=preferencesReady.then(()=>native('notice.state')).then(state=>{if(state.accepted&&(!window.__safeCodeAcceptance||window.__safeCodeConnectionAcceptance)){showScreen('login');confirmServices().then(showPreferences).catch(()=>{});}else{showScreen('notice');}}).catch(()=>{showScreen('notice');});
    startupReady.then(async()=>{
      if(window.__safeCodeAcceptance||window.__safeCodeConnectionAcceptance||!$('#notice-screen').hidden)return;
      const attempt=connectionAttempt;
      await keyAction('status');
      if(attempt!==connectionAttempt)return;
      const prefersApi=keyState.configured&&await native('desktopKey.mode').catch(()=>false);
      if(attempt!==connectionAttempt)return;
      if(prefersApi&&!$('#login-screen').hidden){showApiAccess(false);return;}
      const settings=await native('gateways.resume-settings');
      if(attempt===connectionAttempt&&settings&&!$('#login-screen').hidden&&!connectionInFlight)await connect(settings,true);
    }).catch(()=>{});
    for(const button of document.querySelectorAll('[data-open-services]')){
      if(isMac)button.onclick=()=>native('services.open').catch(error=>showAppMessage(error.message));
      else button.addEventListener('click',refreshPreferences);
    }
    // 版本查询与统计授权相互独立，验收模式不调用外部服务。
    if(!window.__safeCodeAcceptance)setTimeout(()=>checkUpdate(false),1500);
    window.safeCodeRunIpAcceptance=async config=>{
      const checks={};
      try{
        await startupReady;await native('diagnostics.consent',{enabled:false});await native('services.consent',{enabled:false,confirmed:true});await native('notice.accept');
        $('#ip').value=config.host;$('#username').value=config.username;
        const connected=await connect(config);config.password='';if(!connected||connected.osId!=='ubuntu')throw new Error('未连接指定的真实测试目标');
        if(nativeServer.observedEgressIp!==config.egressIp||!nativeServer.ipPolicyHealthy)throw new Error('初始住宅出口基线不健康');checks.initialBaseline=true;
        let rejected=false;try{await native('ip.apply',{ingressIp:config.ingressIp,egressIp:'192.0.2.123',lockIngress:true,lockEgress:true});}catch{rejected=true;}
        if(!rejected)throw new Error('未分配出口没有被拒绝');checks.unassignedRejected=true;
        switchMode('maintenance');openIpPolicy(true);
        $('#ingress-ip-input').value=config.egressIp;$('#egress-ip-input').value=config.egressIp;$('#lock-ingress').checked=true;$('#lock-egress').checked=true;
        const changed=$('#ip-policy-form').onsubmit({preventDefault(){}});if(!ipSaving||!$('#close-ip-policy').disabled)throw new Error('应用等待状态或防重复提交未生效');
        await native('acceptance.capture',{tag:'ip'});await changed;
        if($('#ip-policy-dialog').open||nativeServer.ipPolicy?.ingressIp!==config.egressIp||nativeServer.observedEgressIp!==config.egressIp)throw new Error('更换入口未成功');checks.ingressChanged=true;
        const remembered=await native('login.load');if(remembered?.host!==config.egressIp)throw new Error('新入口没有保存到连接记忆');checks.connectionRemembered=true;
        openIpPolicy(true);$('#ingress-ip-input').value=config.ingressIp;$('#egress-ip-input').value=config.egressIp;$('#lock-ingress').checked=true;$('#lock-egress').checked=true;await $('#ip-policy-form').onsubmit({preventDefault(){}});
        if($('#ip-policy-dialog').open||nativeServer.ipPolicy?.ingressIp!==config.ingressIp||nativeServer.observedEgressIp!==config.egressIp||!nativeServer.ipPolicyHealthy)throw new Error('恢复极速入口与住宅出口失败');checks.baselineRestored=true;
        const inspected=await native('inspect');applyServer(inspected);if(!inspected.ipPolicyHealthy)throw new Error('重新巡检未读到已保存策略');checks.persistedReadback=true;
        await native('acceptance.capture',{tag:'maintenance'});
        await native('acceptance.done',{success:true,checks,ingress:nativeServer.ipPolicy.ingressIp,observedEgress:nativeServer.observedEgressIp,policyHealthy:nativeServer.ipPolicyHealthy});
      }catch(error){await native('acceptance.done',{success:false,checks,error:error.message});}
    };
    window.safeCodeRunUpdateAcceptance=async()=>{
      try{
        await startupReady;showScreen('login');await native('window.resize',{wide:false});
        displayUpdate({currentVersion:'Beta-0.4.3.3',version:'Beta-0.4.3.4',status:'发现新版本（界面预览）',page:null,canInstall:false});
        const notice=$('#title-update-notice'),bounds=notice.getBoundingClientRect(),controls=$('.window-controls').getBoundingClientRect();
        if(notice.hidden||bounds.right>controls.left||bounds.width<60)throw new Error('标题更新提示缺失或与窗口按钮重叠');
        await native('acceptance.capture',{tag:'update'});notice.click();if(!$('#services-dialog').open)throw new Error('标题提示无法打开更新窗口');
        await native('acceptance.capture',{tag:'services'});$('#services-dialog').close();
        await native('acceptance.done',{success:true,titleUpdateNotice:true,clickOpensUpdates:true,footerHighlight:$('#services-entry').classList.contains('has-update'),previewOnly:true});
      }catch(error){await native('acceptance.done',{success:false,error:error.message});}
    };
    window.safeCodeRunServicesAcceptance=async()=>{
      try{
        const windowSizing=await native('acceptance.window-sizes');const prefs=await native('services.preferences');showPreferences(prefs);if(prefs.enabled||prefs.diagnosticsEnabled)throw new Error('首次统计或诊断默认值错误');
        await native('acceptance.capture',{tag:'notice'});if($('#exit').getBoundingClientRect().bottom>innerHeight||$('#usage-consent').getBoundingClientRect().bottom>$('#accept').getBoundingClientRect().top)throw new Error('声明页布局溢出');
        await native('services.consent',{enabled:false,confirmed:false});const disabled=await native('services.preferences');if(disabled.enabled)throw new Error('关闭统计失败');showPreferences(disabled);
        $('#services-dialog').showModal();if($('#feedback-send').getBoundingClientRect().bottom>$('#services-dialog').getBoundingClientRect().bottom)throw new Error('反馈按钮超出窗口');await checkUpdate(true);await native('acceptance.capture',{tag:'services'});const feedbackMarker='SafeCode Windows integration test '+crypto.randomUUID();await native('feedback.send',{message:feedbackMarker});$('#services-dialog').close();
        await native('services.consent',{enabled:true,confirmed:false});
        await native('acceptance.done',{success:true,windowSizing,defaultEnabled:prefs.enabled,optOutSaved:!disabled.enabled,serviceAvailable:prefs.serviceAvailable,recipient:prefs.recipient,feedbackMarker,feedbackSentWithStatsDisabled:true,updateStatus:$('#update-status').textContent,viewportHeight:innerHeight});
      }catch(error){await native('acceptance.done',{success:false,error:error.message});}
    };
    window.safeCodeRunConnectionAcceptance=async config=>{
      if(config.previewOnly){
        try{
          await startupReady;
          showScreen('login');$('#ip').value='127.0.0.1';$('#ip').dispatchEvent(new Event('input'));$('#username').value='';$('#password').value='';
          await $('#login-form').onsubmit({preventDefault(){}});
          if($('#workspace-screen').hidden||!$('#developer-preview'))throw new Error('开发预览没有正常进入');
          switchMode('maintenance');if($('#maintenance-panel').hidden)throw new Error('维护模式不可见');
          await native('acceptance.capture',{tag:'maintenance'});
          await native('acceptance.done',{success:true,developerPreview:true,noServer:true});
        }catch(error){await native('acceptance.done',{success:false,error:error.message});}return;
      }

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
        await startupReady;const result=await native('acceptance.connect-current');
        applyDeploymentSettings(await native('deployment.settings.get'));
        if(!currentLocale())throw new Error('工作账户更新验收需要先为该连接手动保存本土化设置。');
        $('#username').value=result.username;nativeServer=result.server;connectionReady=true;busy=false;showScreen('workspace');lockWorkspace(false);applyServer(result.server);
        const before=await native('acceptance.hostname');await openAccountSettings();$('#custom-hostname').value='safe-draft-verification';$('#custom-username').value=result.server.username;$('#custom-password').value='fixture-password-1234';
        const saving=$('#account-settings-form').onsubmit({preventDefault(){}});const warningDeadline=Date.now()+5000;while(!$('#app-message-dialog').open&&Date.now()<warningDeadline)await new Promise(resolve=>setTimeout(resolve,20));if(!$('#app-message-dialog').open)throw new Error('再次更改账户未弹出确认');$('#app-message-cancel').click();await saving;
        if(await native('acceptance.hostname')!==before)throw new Error('取消更改仍修改了服务器');
        // 后续只验证草稿存储；真实应用流程由隔离 UI 夹具覆盖，不将示例密码应用到验收服务器。
        await native('account.draft.save',{username:result.server.username,hostname:'safe-draft-verification',password:'fixture-password-1234'});accountOptions={username:result.server.username,hostname:'safe-draft-verification',password:'fixture-password-1234'};accountDraftPending=true;accountSettingsSaved=true;$('#custom-password').value='';$('#account-settings').close();renderTasks();
        if(!accountDraftPending||states[0]!=='done'||$('#steps .step:first-child > .deploy-button:not(.locale-settings-button)').textContent!=='更新'||$('#steps .step:first-child > .deploy-button:not(.locale-settings-button)').disabled)throw new Error('已完成步骤未提供更新');
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
    if(isMac){
      for(const element of document.querySelectorAll('.host-permission-note p'))element.textContent=element.textContent.replaceAll('Windows','macOS');
      document.querySelector('.host-risk small').textContent='需先在系统设置中开启远程登录，仅允许当前账户。使用专用密钥，不会修改系统 SSH 设置。';
      $('#host-alias').placeholder='例如 mac 或 local';
      renderClaudeBridge();
      $('#bridge-manage').title='查看第三方功能警告';
      renderDesktopAccess();
    }
    window.safeCodeNativeReady=true;
  })();
}
