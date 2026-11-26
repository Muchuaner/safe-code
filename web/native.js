/* Windows 客戶端橋接。普通瀏覽器繼續使用概念稿；桌面模式不執行模擬部署。 */
if (window.chrome?.webview && (location.host === 'safe-code.local' || (window.__safeCodePlatform === 'macos' && location.protocol === 'file:'))) {
  (() => {
    const isMac=window.__safeCodePlatform==='macos';
    const servicesWindow=isMac&&window.__safeCodeServicesWindow;
    const pending = new Map();
    const native = (op,args={},deadline) => new Promise((resolve,reject) => {
      if(developerPreview&&!/^(window\.|services\.|diagnostics\.|notice\.|updates\.|feedback\.|copy$|mount\.status\.open$|preview\.|acceptance\.|claudeDesktop\.open$|desktopKey\.|codex\.open$)/.test(op))return reject(new Error('開發預覽未連接伺服器，此操作僅在真實連線後可用。'));
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
    const consentIds=['usage-consent','settings-usage-consent','diagnostics-consent','settings-diagnostics-consent'];
    let servicePreferences=null, update=null;
    function showPreferences(p){
      servicePreferences=p;
      for(const id of consentIds)document.getElementById(id).checked=id.includes('diagnostics')?!!p.diagnosticsEnabled:!!p.enabled;
      $('#usage-recipient').textContent=p.serviceAvailable?'接收方：'+p.recipient+'。服務資料庫不儲存來源 IP；網絡連線會向接收伺服器展示來源 IP。':'回饋與統計服務尚未啟用，目前不會上載。';
      $('#feedback-send').disabled=!p.serviceAvailable;
      $('#feedback-status').textContent=p.issueId?'本次回饋將關聯最近失敗的問題編號：'+p.issueId:'提交你填寫的文字及軟件版本，當前沒有可關聯的失敗診斷。';
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
      $('#update-status').textContent='當前版本 '+info.currentVersion+' · '+info.status;
      $('#release-page').hidden=!info.page;$('#download-update').hidden=!info.canInstall;
      showUpdateNotice(info);
    }
    function showUpdateNotice(info){
      const available=!!info.version;
      $('#services-entry').textContent=available?'發現新版本':'更新與回饋';$('#services-entry').classList.toggle('has-update',available);
      const notice=$('#title-update-notice');notice.hidden=!available;notice.title=available?'發現新版本 '+info.version+'，點擊查看更新':'開啟更新與回饋';notice.setAttribute('aria-label',notice.title);
    }
    async function checkUpdate(force=false){$('#check-update').disabled=true;$('#update-status').textContent='正在檢查 GitHub…';try{displayUpdate(await native('updates.check',{force}));}catch(e){$('#update-status').textContent='檢查失敗：'+e.message;}finally{$('#check-update').disabled=false;}}
    $('#check-update').onclick=()=>checkUpdate(true);
    $('#release-page').onclick=()=>{if(update?.page)window.open(update.page,'_blank');};
    window.safeCodeUpdateProgress=(done,total)=>{$('#update-status').textContent='正在下載 '+Math.round(done/total*100)+'%';};
    $('#download-update').onclick=async()=>{$('#download-update').disabled=true;try{await native('updates.download');$('#update-status').textContent='下載完成，SHA256 校驗通過。'+(isMac?'請先安全卸載目錄，再開啟鏡像替換應用。':'安裝時會關閉當前客戶端。');$('#download-update').hidden=true;$('#install-update').hidden=false;}catch(e){$('#update-status').textContent=e.message;}finally{$('#download-update').disabled=false;}};
    $('#install-update').onclick=()=>{if(busy){$('#update-status').textContent='請等待當前操作完成後安裝更新。';return;}native('updates.install').catch(e=>{$('#update-status').textContent=e.message;});};
    $('#feedback-form').onsubmit=async e=>{e.preventDefault();$('#feedback-send').disabled=true;try{await native('feedback.send',{message:$('#feedback-message').value});$('#feedback-message').value='';$('#feedback-status').textContent='已提交，謝謝你的回饋。';}catch(e){$('#feedback-status').textContent=e.message;}finally{$('#feedback-send').disabled=!servicePreferences?.serviceAvailable;}};

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
    const requiredGatewayRevision=23;
    let publicGatewayTask,publicGatewayTimer;
    let publicGatewaySubscription;
    let startupRetryTimer;
    let keyState={configured:false,running:false}, keyBusy=false;
    let currentTask = -1;
    let nativeServer = null;
    let sshPort = 22;
    let metricsTask = Promise.resolve();
    let metricsTimer=null;let metricsBusy=false;
    let resourceTimer=null;let resourceBusy=false;
    let environmentTimer=null;let environmentBusy=false;
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
    $('#advanced-form').onsubmit=e=>{e.preventDefault();const input=$('#ssh-port');const port=Number(input.value);input.setCustomValidity(Number.isInteger(port)&&port>=1&&port<=65535?'':'SSH 連接埠必須在 1 到 65535 之間。');if(!$('#advanced-form').reportValidity())return;sshPort=port;$('#advanced-dialog').close();};
    async function beginPreview(){
      await native('preview.begin');
      developerPreview=true;connectionAttempt++;clearInterval(metricsTimer);clearInterval(resourceTimer);clearInterval(environmentTimer);connectionReady=false;connectionInFlight=false;nativeServer=null;
      resetHostControl();
      $('#advanced-dialog').close();enterDeveloperPreview();lockWorkspace(false);$('#connection-progress').hidden=true;
      setMetrics(null);for(const value of $$('.network-top strong'))value.textContent='—';
      setResources(null,'預覽模式未連接伺服器，不顯示模擬的監控數值。');
      setEnvironmentAssessment(null,'未連線');
      $('.server-metadata').textContent='介面預覽 / 未連接伺服器';
    }
    $('#enter-preview').onclick=async()=>{try{await beginPreview();}catch(error){await showAppMessage(error.message);}};
    $('#login-help').onclick=async()=>{if(!window.__safeCodeNativeDialogs)await native('window.resize',{wide:true});$('#login-help-dialog').showModal();};
    $('#login-help-dialog').addEventListener('close',()=>{if(!window.__safeCodeNativeDialogs&&!$('#login-screen').hidden)native('window.resize',{wide:false});});
    function applyServer(server){
      const hadAccount=!!nativeServer?.statuses?.account;
      nativeServer=server;$('#update-login-password').hidden=false;$('#host-manage').disabled=!server.statuses?.account;states=ids.map(id=>server.statuses?.[id]?'done':'pending');
      if(currentTask>=0&&states[currentTask]!=='done')states[currentTask]='running';
      const desired=currentLocale();if(desired&&(server.timezone!==desired.timezone||server.systemLocale!==desired.locale))states[1]='pending';
      if(server.ipPolicy){ipPolicy=server.ipPolicy;$('#ip-policy-state').textContent=server.ipPolicyHealthy?(ipPolicy.lockIngress&&ipPolicy.lockEgress?'已鎖定':ipPolicy.lockIngress||ipPolicy.lockEgress?'部分鎖定':'未鎖定'):'待檢查';}
      else{ipPolicy={lockIngress:false,lockEgress:false};$('#ip-policy-state').textContent='未配置';}
      const assigned=(server.assignedIps||[]).filter(isPublicIpv4);setDetectedIpOptions(assigned);
      if(server.observedEgressIp){serverConnection.egressIp=server.observedEgressIp;$('#server-address').textContent=server.observedEgressIp;}
      if(server.ipPolicy)serverConnection.ingressIp=server.ipPolicy.ingressIp;
      $('#ip-policy-summary').textContent=server.ipPolicy?.lockEgress?'住宅出口已固定；外部檢測：'+(server.observedEgressIp||'待確認'):'未固定出口，當前實際出口：'+(server.observedEgressIp||'待確認');
      updateSocksCard(server.socksEgress);
      $('#server-user').textContent=$('#username').value.trim();
      const meta=$('.server-metadata');const user=$('#server-user');meta.replaceChildren(user,document.createTextNode(' / Ubuntu '+server.osVersion+' / SSH'));
      $('#mount-state').textContent=server.mountReady?'已安裝':'未安裝';$('#install-mount').disabled=server.mountReady;$('#install-mount').textContent=server.mountReady?'已安裝':'安裝';
      $('#cloud-state').textContent=server.cloudReady?'已就緒':'未安裝';$('#install-cloud').disabled=server.cloudReady;$('#install-cloud').textContent=server.cloudReady?'已安裝':'安裝';$('#open-cloud').disabled=!server.cloudReady;if($('#open-cloud-remote'))$('#open-cloud-remote').disabled=!server.cloudReady;
      renderTasks();
      if(connectionReady&&!hadAccount&&server.statuses?.account)void refreshPublicGateway();
    }
    function setMetrics(stats){
      const values=$$('.network-top strong');
      values[0].textContent=stats?(stats.latencyMs===null?'未響應':Math.round(stats.latencyMs)+' ms'):'檢測中';
      values[1].textContent=stats?stats.lossPercent+' %':'—';
      $('.network-top').title='每 5 秒探測一次；丟包率為最近 20 次 ICMP 探測統計。ICMP 未響應不代表 SSH 不可達。';
    }
    function setResources(stats,message){
      const gib=bytes=>(bytes/1024**3).toFixed(1);
      $('#resource-cpu').textContent=stats?stats.cpuPercent.toFixed(1)+' %':'—';
      $('#resource-memory').textContent=stats?gib(stats.memoryUsedBytes)+' / '+gib(stats.memoryTotalBytes)+' G':'— / — G';
      $('#resource-disk').textContent=stats?gib(stats.diskUsedBytes)+' / '+gib(stats.diskTotalBytes)+' G':'— / — G';
      $('#server-resources').title=message||'伺服器實時監控，每5秒更新；記憶體按已用量顯示（扣除可回收快取），硬碟為根分區；容量按 GiB 顯示。';
    }
    function startResourceMonitor(attempt){
      const refresh=async()=>{
        if(resourceBusy||!connectionReady||developerPreview||attempt!==connectionAttempt||document.hidden||$('#workspace-screen').hidden)return;
        resourceBusy=true;
        try{const stats=await native('resources');if(attempt===connectionAttempt&&connectionReady&&!developerPreview)setResources(stats);}
        catch{if(attempt===connectionAttempt&&connectionReady&&!developerPreview)setResources(null,'資源監控暫不可用，下次自動重試。');}
        finally{resourceBusy=false;}
      };
      clearInterval(resourceTimer);void refresh();resourceTimer=setInterval(refresh,5000);
    }
    function setEnvironmentAssessment(result,message){
      const output=$('#environment-rating-result');
      const complete=Number.isInteger(result?.riskScore)&&result.riskScore>=0&&result.riskScore<=100;
      output.textContent=complete?result.message+' · '+result.riskScore+'/100':result?.message||message||'暫不可用';
      output.dataset.level=complete?result.level:'unavailable';
      $('.environment-rating').title='在VPS工作用戶的Chrome環境檢測，每分鐘重新整理。風險分越低越好，屬於非官方估計。'+(result?.username?'\nVPS用戶：'+result.username:'')+(result?.signals?.length?'\n'+result.signals.map(s=>s.label+'：'+s.value+'（+'+s.contribution+'）').join('\n'):'');
    }
    function startEnvironmentMonitor(attempt){
      const refresh=async()=>{
        if(environmentBusy||!connectionReady||developerPreview||attempt!==connectionAttempt||document.hidden||$('#workspace-screen').hidden)return;
        environmentBusy=true;
        try{const result=await native('environment.assess');if(attempt===connectionAttempt&&connectionReady&&!developerPreview)setEnvironmentAssessment(result);}
        catch{if(attempt===connectionAttempt&&connectionReady&&!developerPreview)setEnvironmentAssessment(null,'暫不可用');}
        finally{environmentBusy=false;}
      };
      clearInterval(environmentTimer);setEnvironmentAssessment(null,'檢測中…');void refresh();environmentTimer=setInterval(refresh,60000);
    }
    function $$(selector){return [...document.querySelectorAll(selector)];}
    queryIpInfo=async function(ip){
      const attempt=connectionAttempt;
      const current=()=>attempt===connectionAttempt&&ip===serverConnection.egressIp;
      if(!isPublicIpv4(ip)){$('#ip-location').textContent='暫無歸屬地';$('#ip-score').textContent='—';$('#score-unit').textContent='';$('#rating-state').textContent='';return;}
      $('#rating-state').textContent='查詢中';$('#ip-location').textContent='正在查詢…';$('#ip-score').textContent='—';$('#score-unit').textContent='';
      try{
        const info=await native('ip.info',{ip});
        if(!current())return;
        $('#ip-location').textContent=info.location||'暫無歸屬地';$('#ip-score').textContent=info.score===null?'—':String(info.score);$('#score-unit').textContent=info.score===null?'':' / 100';$('#rating-state').textContent=info.geoSource;
        $('#score-source').title='定位：'+info.geoSource+(info.hasGeoConflict?'（部分來源存在分歧）':'')+'；評分為第三方'+(info.isSubnetScore?'網段':'IP')+'評分。';
      }catch{if(current()){$('#rating-state').textContent='暫不可用';$('#ip-location').textContent='暫無歸屬地';$('#ip-score').textContent='—';}}
    };
    let connectionAttempt=0;let connectionReady=false;let connectionInFlight=false;let lastSettings=null;
    function applyDeploymentSettings(settings){
      accountSettingsSaved=settings?.accountSaved===true;
      localeSelection=Object.hasOwn(localePresets,settings?.localeRegion)?settings.localeRegion:'';
    }
    function lockWorkspace(locked){for(const id of ['deployment-panel','maintenance-panel'])$( '#'+id).inert=locked;$('#workspace-screen').classList.toggle('is-connecting',locked);$('#maintenance-mode').disabled=locked;}
    function connectionProgress(text,failed=false){$('#connection-progress').hidden=false;$('#connection-progress').classList.toggle('failed',failed);$('#connection-progress-text').textContent=text;$('#cancel-connect').hidden=failed;$('#retry-connect').hidden=!failed;$('#edit-connection').hidden=!failed;if(failed){busy=false;renderTasks();const values=$$('.network-top strong');for(const value of values)value.textContent='—';$('#rating-state').textContent='未連線';$('#ip-location').textContent='等待連接伺服器';$('#server-address').textContent='待確認';}}
    function prepareWorkspace(settings){
      clearTimeout(publicGatewayTimer);
      publicGatewayEnabled=false;publicGatewayReachable=false;publicGatewayLoading=false;publicGatewayMissing=false;
      publicGatewayUpgrade=false;publicGatewayNeedsCloudKey=false;
      publicGatewayOperation=publicGatewayStatusError='';
      publicGatewayError=undefined;
      publicGatewayCreatedAccount=undefined;
      $('#gateway-repair-dialog').close();
      $('#claude-desktop-dialog').close();
      publicGatewaySubscription=undefined;
      $('#public-gateway-footer').hidden=true;
      clearInterval(metricsTimer);clearInterval(resourceTimer);clearInterval(environmentTimer);setResources(null,'等待伺服器連線完成。');setEnvironmentAssessment(null,'等待連線');$('#update-login-password').hidden=true;nativeServer=null;connectionReady=false;connectionInFlight=true;busy=false;currentTask=-1;accountOptions={};resetDeploymentSettings();ipPolicy={lockIngress:false,lockEgress:false};
      resetHostControl();
      $('#account-settings').close();$('#localization-dialog').close();
      states=Array(tasks.length).fill('pending');taskLogs=Array(tasks.length).fill('等待連線完成。');selectedTasks.clear();selecting=false;renderTasks();
      serverConnection={ingressIp:settings.host,egressIp:''};$('#server-ingress').textContent=settings.host;$('#server-address').textContent='正在檢查…';$('#server-user').textContent=settings.username;
      const user=$('#server-user');$('.server-metadata').replaceChildren(user,document.createTextNode(' / 系統待檢查 / SSH'));
      $('#ip-location').textContent='等待連線';$('#ip-score').textContent='—';$('#score-unit').textContent='';$('#rating-state').textContent='待檢查';setMetrics(null);
      switchMode('deployment');lockWorkspace(true);showScreen('workspace');connectionProgress('正在連接伺服器…');
    }
    function confirmServerIdentity(response){const changed=!!response.key.previousFingerprint;
      return appDialog(changed?'檢測到伺服器密鑰已經變化。\n您是否重新安裝過這台伺服器？\n\n剛剛重新安裝過，可選擇“是”重新信任並連線；沒有重新安裝請選擇“否”並核實伺服器。':'首次連接這台伺服器。\n請核對伺服器身份，確認後建立連線。',{
            title:changed?'是否重新安裝過系統？':'確認伺服器身份',confirm:changed?'是，重新連線':'信任並連線',cancel:changed?'否，取消':'取消',
            details:'伺服器：'+response.key.host+':'+response.key.port+(changed?'\n原指紋：'+response.key.previousFingerprint:'')+'\n新指紋：'+response.key.fingerprint});
    }
    async function connect(settings, automatic=false, retries=0){
      clearTimeout(startupRetryTimer);
      const attempt=++connectionAttempt;lastSettings=settings;
      if(automatic){$('#ip').value=settings.host;$('#username').value=settings.username;sshPort=settings.port||22;}
      $('#password').type='password';setEye($('#reveal'),false);prepareWorkspace(settings);
      // 先呈現主介面，再開始 SSH；慢連線、身份確認和評分均留在主介面處理。
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
            connectionProgress('伺服器暫不可達，'+delay+' 秒後自動重連。',true);
            $('#cancel-connect').hidden=false;
            startupRetryTimer=setTimeout(()=>{
              if(attempt===connectionAttempt)void connect(settings,true,retries+1);
            },delay*1000);
            return null;
          }
          if(!response.requiresTrust)break;
          if(automatic)throw new Error('伺服器身份需要核對，請點擊「重試」確認後連線。');
          const changed=!!response.key.previousFingerprint;
          connectionProgress(changed?'等待確認：伺服器密鑰已變化。':'等待確認伺服器身份。');
          const accepted=await confirmServerIdentity(response);
          if(attempt!==connectionAttempt)return null;
          if(!accepted){connectionInFlight=false;await native('connection.cancel');connectionProgress('已取消連線，原信任記錄未修改。',true);return null;}
          await native('host.trust',{token:response.token});connectionProgress('正在重新連接伺服器…');
        }
        const result=response.server;
        const [saved,draft]=await Promise.all([
          native('deployment.settings.get').catch(()=>null),native('account.draft.get').catch(()=>null)
        ]);
        if(attempt!==connectionAttempt)return null;
        applyDeploymentSettings(saved);accountOptions=draft||{};accountDraftPending=!!draft;
        $('#password').value='';serverConnection={ingressIp:result.ipPolicy?.ingressIp||settings.host,egressIp:result.ipPolicy?.egressIp||result.observedEgressIp||''};$('#server-ingress').textContent=serverConnection.ingressIp;$('#server-address').textContent=serverConnection.egressIp||'待確認';
        taskLogs=Array(tasks.length).fill('');busy=false;connectionReady=true;connectionInFlight=false;lockWorkspace(false);applyServer(result);
        $('#connection-progress').hidden=true;
        const measure=async()=>{if(metricsBusy||!connectionReady)return;metricsBusy=true;try{setMetrics(await native('metrics'));}catch{setMetrics({latencyMs:null,lossPercent:100});}finally{metricsBusy=false;}};metricsTask=measure();metricsTimer=setInterval(measure,5000);
        startResourceMonitor(attempt);
        startEnvironmentMonitor(attempt);
        // 評分不會阻塞連線完成或部署操作。
        void queryIpInfo(serverConnection.egressIp);
        await pollClaudeBridge();
        if(response.gatewayWarnings?.length)void showAppMessage(response.gatewayWarnings.join('\n'),{title:'網關未恢復'});
        if(result.activeJobs?.length)resumeJob(result.activeJobs[0]);return result;
      }catch(error){
        if(attempt!==connectionAttempt)return null;
        connectionReady=false;connectionInFlight=false;lockWorkspace(true);connectionProgress(error.message,true);
        await showAppMessage(error.message,{title:'連線未完成'});return null;
      }finally{if(attempt===connectionAttempt)resetLogin();}
    }
    $('#cancel-connect').onclick=async()=>{clearTimeout(startupRetryTimer);connectionAttempt++;resetHostControl();connectionInFlight=false;connectionProgress('正在取消連線…');await native('connection.cancel');connectionProgress('已取消連線。',true);};
    $('#edit-connection').onclick=()=>{clearTimeout(startupRetryTimer);connectionAttempt++;resetHostControl();native('connection.cancel');showScreen('login');resetLogin();};
    $('#retry-connect').onclick=()=>{if(lastSettings)void connect(lastSettings);};
    $('#login-form').onsubmit=async e=>{
      e.preventDefault();
      const host=$('#ip').value.trim();
      const username=$('#username').value.trim();
      const password=$('#password').value;
      if(isDeveloperHost(host)){
        try{await beginPreview();}catch(error){await showAppMessage(error.message);}return;
      }
      if(!host||!username){
        $('#connection-message').textContent='請填寫 IP 地址和用戶名。';
        $('#connection-message').className='connection-message error';
        return;
      }
      await connect({host,username,password,port:sshPort});
    };
    async function resumeJob(job){
      busy=true;const advanced=job.operation==='cloud'||job.operation==='mount_install';if(advanced)beginOperation(job.operation,job.operation==='cloud'?'恢復 CloudCLI 安裝進度':'恢復目錄掛載安裝進度');currentTask=ids.indexOf(job.operation);
      if(currentTask>=0){states[currentTask]='running';taskLogs[currentTask]='恢復遠端部署監控…';}renderTasks();
      try{applyServer(await native('job.resume',job));if(advanced){appendOperationLog('背景安裝任務已完成。');advancedOperation.status='安裝完成。';}}
      catch(e){if(advanced){appendOperationLog('安裝未完成：'+e.message);advancedOperation.status='安裝未完成，請查看日誌後重試。';}else showAppMessage(e.message);}
      finally{if(advanced){advancedOperation.done=true;if(displayedOperation===advancedOperation)$('#operation-log-status').textContent=advancedOperation.status;}currentTask=-1;busy=false;renderTasks();}
    }
    async function runTask(index){
      if(!connectionReady||!deploymentSettingsReady())throw new Error('請先儲存工作用戶和本土化設定。');
      const previousState=states[index];currentTask=index;states[index]='running';taskLogs[index]='正在連接 Ubuntu 部署模組…';renderTasks();
      try{
        const preset=currentLocale();
        const locale=preset?{region:preset.name,timezone:preset.timezone,locale:preset.locale,language:preset.language}:null;
        const result=await native('deploy',{step:ids[index],locale,username:accountOptions.username||nativeServer?.username||'workspace',hostname:accountOptions.hostname||'',password:accountOptions.password||''});applyServer(result);
        if(!result.statuses?.[ids[index]])throw new Error(tasks[index][0]+'驗證未通過，請查看日誌。');
        taskLogs[index]='部署完成，已通過實際狀態檢查。';if(index===0){accountOptions={};accountDraftPending=false;}
      }catch(error){states[index]=index===0&&previousState==='done'?'done':'pending';taskLogs[index]='失敗：'+error.message;throw error;}
      finally{currentTask=-1;renderTasks();}
    }
    deployOne=async function(index){if(busy||!connectionReady||!deploymentSettingsReady()||!canDeployTask(index))return;selecting=false;selectedTasks.clear();busy=true;try{await runTask(index);}catch(error){showAppMessage(error.message);}finally{busy=false;renderTasks();}};
    $('#deploy-all').onclick=async()=>{
      if(busy||!connectionReady||!deploymentSettingsReady())return;if(!selecting){selecting=true;selectedTasks.clear();renderTasks();return;}
      const queue=[...selectedTasks].filter(i=>states[i]!=='done').sort((a,b)=>a-b);if(!queue.length)return;
      selecting=false;selectedTasks.clear();busy=true;
      try{for(const i of queue)await runTask(i);}catch(error){showAppMessage(error.message);}finally{busy=false;renderTasks();}
    };
    $('#account-card').onclick=async()=>{
      try{
        const account=await native('account');$('#result-body').replaceChildren();
        for(const [name,value] of [['用戶名',account.username],['密碼',account.password]]){
          const row=document.createElement('div');row.className='credential-row';const label=document.createElement('span');label.textContent=name;
          const group=document.createElement('div');group.className='credential-value';const code=document.createElement('code');code.textContent=value;
          const copy=document.createElement('button');copy.className='copy-button';copy.setAttribute('aria-label','複製'+name);copy.textContent='⧉';copy.onclick=async()=>{await native('copy',{text:value});copy.title='已複製';setTimeout(()=>copy.title='複製',1200);};group.append(code,copy);row.append(label,group);$('#result-body').append(row);
        }
        $('#maintenance-result').showModal();
      }catch(error){showAppMessage(error.message);}
    };
    async function runMaintenanceCard(selector,action){const card=$(selector);const caption=card.querySelector('span');const text=caption.textContent;card.disabled=true;caption.textContent='正在連線…';try{await action();}catch(e){showAppMessage(e.message);}finally{card.disabled=false;caption.textContent=text;}}
    $('#desktop-card').onclick=()=>runMaintenanceCard('#desktop-card',()=>native('desktop'));
    $('#terminal-card').onclick=()=>runMaintenanceCard('#terminal-card',async()=>{const result=await native('terminal');if(result)applyServer(result);});
    let advancedOperation=null;let displayedOperation=null;const operationHistory=new Map();
    function appendOperationLog(line){
      if(!advancedOperation)return;advancedOperation.lines.push(line);if(advancedOperation.lines.length>300)advancedOperation.lines.shift();
      const key=advancedOperation.key==='cloud'?'cloud':'mount';$('#'+key+'-live-log').hidden=false;$('#'+key+'-live-log').textContent='查看日誌：'+line;
      if(displayedOperation===advancedOperation){const box=$('#operation-log-output');box.textContent=advancedOperation.lines.join('\n');box.scrollTop=box.scrollHeight;}
    }
    function beginOperation(key,label){advancedOperation={key,label,lines:[],started:Date.now(),done:false,status:'正在準備…'};operationHistory.set(key,advancedOperation);displayedOperation=advancedOperation;$('#operation-log-title').textContent=label;$('#operation-log-status').textContent='正在準備…';$('#operation-log-output').textContent='';$('#operation-log-dialog').showModal();appendOperationLog('已開始，請等待伺服器返回實際進度。');}
    function syncAdvancedButtons(){
      if(document.querySelector('#bridge-manage'))$('#bridge-manage').disabled=busy||bridgeBusy||(!isMac&&!nativeServer?.statuses?.account);
      if(document.querySelector('#host-manage'))$('#host-manage').disabled=busy||hostBusy||!nativeServer?.statuses?.account;
      renderDesktopAccess();
      for(const [key,ready] of [['mount',nativeServer?.mountReady],['cloud',nativeServer?.cloudReady]]){const button=$('#install-'+key);button.disabled=!!ready||busy;button.title=busy?'有任務正在執行，請查看進度。':'';button.textContent=advancedOperation&&!advancedOperation.done&&advancedOperation.key===(key==='cloud'?'cloud':'mount_install')?'安裝中…':ready?'已安裝':'安裝';}
      if($('#open-cloud-remote'))$('#open-cloud-remote').disabled=!nativeServer?.cloudReady||busy;
    }
    for(const key of ['mount','cloud']){const log=document.createElement('p');log.id=key+'-live-log';log.className='advanced-live-log';log.hidden=true;$('#'+key+'-summary')?.after(log);if(!log.isConnected)$('#install-'+key).closest('.advanced-card').querySelector('.advanced-copy>div').append(log);log.title='點擊查看安裝日誌';log.onclick=()=>{const op=operationHistory.get(key==='cloud'?'cloud':'mount_install');if(op){displayedOperation=op;$('#operation-log-title').textContent=op.label;$('#operation-log-status').textContent=op.status;$('#operation-log-output').textContent=op.lines.join('\n');$('#operation-log-dialog').showModal();}};}
    setInterval(()=>{if(advancedOperation&&!advancedOperation.done)$('#operation-log-status').textContent='正在安裝，已用時 '+Math.floor((Date.now()-advancedOperation.started)/1000)+' 秒。'},1000);
    async function installAdvanced(key){
      if(busy){if(advancedOperation&&!advancedOperation.done)$('#operation-log-dialog').showModal();else showAppMessage('已有任務正在執行，請等待完成。');return;}
      busy=true;const id=key==='cloud'?'cloud':'mount_install';beginOperation(id,key==='cloud'?'安裝 CloudCLI 網頁介面':'安裝目錄掛載組件');renderTasks();syncAdvancedButtons();
      try{applyServer(await native(key==='cloud'?'cloud.install':'mount.install'));appendOperationLog('安裝完成，已通過伺服器狀態檢查。');advancedOperation.status='安裝完成。';if(displayedOperation===advancedOperation)$('#operation-log-status').textContent=advancedOperation.status;}
      catch(e){appendOperationLog('安裝失敗：'+e.message);advancedOperation.status='安裝未完成，請查看日誌後重試。';if(displayedOperation===advancedOperation)$('#operation-log-status').textContent=advancedOperation.status;$('#'+key+'-state').textContent='安裝失敗';}
      finally{advancedOperation.done=true;busy=false;renderTasks();syncAdvancedButtons();}
    }
    $('#install-cloud').onclick=()=>installAdvanced('cloud');$('#install-mount').onclick=()=>installAdvanced('mount');
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
      $('#cloud-remote-log-status').textContent='正在配置並開放公網訪問…';
      $('#cloud-remote-log-output').textContent='已確認風險，正在連接伺服器執行配置…\n';
      cloudRemoteRunning=true;busy=true;renderTasks();
      try{
        const res=await native('cloud.expose');
        const url=(res&&res.url)?res.url:('http://'+(res?.ip||serverConnection.ingressIp)+':3001');
        cloudRemoteUrl=url;
        window.showCloudRemoteSuccess?.(url);
      }catch(err){
        $('#cloud-remote-log-status').textContent='開放公網訪問失敗：'+err.message;
        appendCloudRemoteLog('錯誤：'+err.message);
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
      const epoch=++folderEpoch;lastFolderPath=path;clearInterval(folderTimer);$('#folder-log').textContent='';$('#folder-error').textContent='';$('#folder-list').textContent='正在讀取…';$('#folder-confirm').disabled=true;$('#folder-new').disabled=true;$('#folder-up').disabled=true;$('#folder-retry').hidden=true;
      $('#folder-path').textContent=path||(nativeServer?.homeDirectory?nativeServer.homeDirectory+'/workspace':'工作目錄');appendFolderLog('正在透過 SSH 讀取工作用戶目錄…');const started=Date.now();
      folderTimer=setInterval(()=>appendFolderLog('仍在等待伺服器響應，已用時 '+Math.floor((Date.now()-started)/1000)+' 秒。'),3000);
      try{
        const result=await native('folder.list',{path});if(epoch!==folderEpoch)return;
        lastFolderPath=result.path;$('#folder-path').textContent=result.path;$('#folder-up').disabled=result.path===result.parent;$('#folder-up').onclick=()=>listRemote(result.parent);$('#folder-list').replaceChildren();
        for(const dir of result.directories){const button=document.createElement('button');button.textContent=dir.name;button.onclick=()=>listRemote(dir.path);$('#folder-list').append(button);}
        if(!result.directories.length)$('#folder-list').textContent='此目錄沒有下級資料夾。';$('#folder-confirm').disabled=false;$('#folder-new').disabled=false;appendFolderLog('讀取完成：'+result.directories.length+' 個目錄。'+(result.truncated?'僅顯示前 300 個，可手動填寫更具體路徑。':''));
      }catch(e){if(epoch!==folderEpoch)return;$('#folder-list').textContent='';$('#folder-error').textContent=e.message;appendFolderLog('讀取未完成，可點擊重試或返回上一級。');$('#folder-retry').hidden=false;const current=$('#folder-path').textContent;if(current.startsWith('/')&&current!=='/'){const parent=current.slice(0,current.lastIndexOf('/'))||'/';$('#folder-up').disabled=false;$('#folder-up').onclick=()=>listRemote(parent);}}
      finally{if(epoch===folderEpoch)clearInterval(folderTimer);}
    }
    let creatingFolder=false;
    $('#folder-new').onclick=()=>{if($('#folder-confirm').disabled)return;$('#create-folder-name').value='';$('#create-folder-error').textContent='';$('#create-folder-parent').textContent='建立位置：'+$('#folder-path').textContent;$('#create-folder-dialog').showModal();$('#create-folder-name').focus();};
    $('#create-folder-dialog').addEventListener('cancel',event=>{if(creatingFolder)event.preventDefault();});
    $('#create-folder-form').onsubmit=async event=>{
      event.preventDefault();if(creatingFolder)return;const name=$('#create-folder-name').value.trim();
      if(!name||name==='.'||name==='..'||/[\\/\x00-\x1f]/.test(name)){$('#create-folder-error').textContent='請輸入有效的資料夾名稱，不能包含路徑分隔符。';return;}
      creatingFolder=true;const button=$('#create-folder-form [type=submit]');const controls=[...$('#create-folder-dialog').querySelectorAll('button,input')];for(const control of controls)control.disabled=true;button.textContent='正在建立…';
      try{const result=await native('folder.create',{path:$('#folder-path').textContent,name});if(selectedRemoteField)selectedRemoteField.value=result.path;$('#create-folder-dialog').close();$('#remote-folder-dialog').close();}
      catch(error){$('#create-folder-error').textContent=error.message;}
      finally{creatingFolder=false;for(const control of controls)control.disabled=false;button.textContent='建立並選擇';}
    };
    window.safeCodeMountStatus=status=>{
      const button=$('#mount-guard-status');button.textContent=status.count?'掛載 '+status.count+' 個目錄 · '+(status.state==='connected'?'守護中':status.state==='attention'?'需要處理':'重連中'):'掛載守護 · 未掛載';button.title=status.message;
      button.classList.toggle('inactive',!status.count||status.state!=='connected'||status.authenticationRequired);
      if(status.authenticationRequired)$('#update-login-password').classList.add('needs-credentials');else $('#update-login-password').classList.remove('needs-credentials');
      if(status.count)$('#mount-summary').textContent='已掛載 '+status.count+' 個目錄，'+(status.state==='connected'?'斷線自動恢復。':status.message+'。');
    };
    $('#mount-guard-status').onclick=()=>native('mount.status.open').catch(error=>showAppMessage(error.message));
    $('#folder-retry').onclick=()=>listRemote(lastFolderPath);
    $('#remote-folder-dialog').addEventListener('close',()=>{folderEpoch++;clearInterval(folderTimer);native('folder.cancel').catch(()=>{});});
    $('#folder-confirm').onclick=()=>{if(selectedRemoteField)selectedRemoteField.value=$('#folder-path').textContent;$('#remote-folder-dialog').close();};
    const originalAppendMountPair=appendMountPair;appendMountPair=function(pair){originalAppendMountPair(pair);const row=$('#mount-list').lastElementChild;for(const key of ['local','remote']){const field=row.querySelector('[data-path='+key+']');if(key==='remote'&&nativeServer?.homeDirectory)field.placeholder=nativeServer.homeDirectory+'/workspace/projects';const wrap=document.createElement('span');wrap.className='folder-input';field.replaceWith(wrap);wrap.append(field);const picker=document.createElement('button');picker.type='button';picker.textContent='選擇目錄';picker.onclick=async()=>{if(key==='local'){try{const selected=await native('pickFolder');if(selected)field.value=selected;}catch(e){showAppMessage(e.message);}}else{selectedRemoteField=field;$('#remote-folder-dialog').showModal();await listRemote();}};wrap.append(picker);}};
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
        if(saved?.localeRegion!==region)throw new Error('伺服器設定未儲存，請重試。');
        applyDeploymentSettings(saved);taskLogs[1]='';selectedTasks.delete(1);applyServer(nativeServer);
        $('#localization-dialog').close();
      }catch(error){if(attempt===connectionAttempt)await showAppMessage(error.message);}
      finally{localeSaving=false;localeRegion.disabled=false;$('#close-localization').disabled=false;updateLocalePreview();}
    };
    openAccountSettings=async function(){
      const attempt=connectionAttempt;
      try{const draft=await native('account.draft.get',isMac?{interactive:true}:{});if(attempt!==connectionAttempt)return;accountOptions=draft||{};accountDraftPending=!!draft;
        $('#custom-hostname').value=draft?.hostname||nativeServer?.hostname||'safe-workstation';$('#custom-username').value=draft?.username||nativeServer?.username||'workspace';$('#custom-password').type='password';$('#custom-password').value=draft?.password||'';validateWorkPassword();setEye($('#account-settings [data-reveal]'),false);
        $('#account-custom-hint').textContent='工作密碼用於工作用戶和遠端桌面。儲存工作用戶和本土化設定後即可部署。更換用戶名會建立新環境，保留舊項目，需先登出舊桌面並卸載目錄，舊 CloudCLI 將停止。';$('#account-settings').showModal();
      }catch(e){showAppMessage(e.message);}
    };
    const workPassword=$('#custom-password');
    function validateWorkPassword(){
      const password=workPassword.value;
      workPassword.setCustomValidity(password && (password.length<12||password.length>256||password.includes('\0'))
        ? '工作密碼須為 12–256 位，不能包含空字符。' : '');
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
          const confirmed=await appDialog('頻繁更改工作用戶名或密碼，可能導致遠端桌面、SSH 或正在使用的服務連線失敗。請確認你瞭解更改的影響。\n\n這裏修改的是工作用戶，不是 root 的伺服器登入密碼。',{
            title:'確認更改工作帳戶',cancel:'我不改了',confirm:currentLocale()?'我知道自己在做什麼，更改並應用':'確認並儲存參數'});
          if(!confirmed)return;
        }
        if(attempt!==connectionAttempt)return;
        await native('account.draft.save',draft);
        if(attempt!==connectionAttempt)return;
        accountSettingsSaved=true;accountOptions=draft;accountDraftPending=true;$('#custom-password').value='';$('#account-settings').close();
        taskLogs[0]='參數已儲存，等待'+(states[0]==='done'?'更新':'開始部署')+'後應用。';renderTasks();
        if(status?.requiresConfirmation&&deploymentSettingsReady())await deployOne(0);
      }catch(e){await showAppMessage(e.message);}
      finally{accountSaving=false;save.disabled=false;}
    };
    let credentialSaving=false;
    $('#update-login-password').onclick=async()=>{
      if(busy||credentialSaving)return;
      try{const info=await native('connection.info');if(!info)throw new Error('請先登入伺服器。');
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
      $('#credential-error').textContent='正在驗證新密碼並恢復連線…';
      try{
        const password=$('#credential-password').value;$('#credential-password').value='';
        const result=await native('connection.reauthenticate',{password});if(lastSettings)lastSettings.password=password;
        window.safeCodeMountStatus(result.mount);$('#credential-dialog').close();
        void refreshPublicGateway();
        await showAppMessage(result.mount.authenticationRequired?'新密碼已驗證；掛載仍需處理，請查看掛載守護狀態。':'新密碼已驗證，登入資料已更新。'+(result.mount.count?'掛載狀態：'+result.mount.message+'。':''),{title:'登入密碼已更新'});
      }catch(error){$('#credential-error').textContent=error.message+' 原掛載和快取已保留，可重新輸入密碼。';}
      finally{credentialSaving=false;busy=false;for(const control of controls)control.disabled=false;renderTasks();}
    };
    if(isMac){
      $('#mac-rdp-options').hidden=false;
      $('#rdp-display-hint').textContent='下次開啟桌面時生效。使用 SDL FreeRDP 可免重複輸入工作用戶密碼；其他客戶端按其自身設定連線。';
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
        if(result.deferred>0)showAppMessage(isMac?'已儲存，下次開啟遠端桌面時生效。':'視窗大小已調整。當前伺服器未接受即時解像度切換，新像素解像度將在下次開啟桌面時使用。');
      }catch(error){showAppMessage(error.message);}
    };
    function fillSavedLogin(saved){
      if(!saved||!$('#workspace-screen').hidden)return;
      $('#ip').value=saved.host;$('#username').value=saved.username;$('#password').value=saved.password;sshPort=saved.port||22;
    }
    const restore=$('#restore-saved-login');
    async function loadLoginForm(){
      try{
        const saved=await native('login.load');
        if(saved){
          if(!['#ip','#username','#password'].some(id=>$(id).value))fillSavedLogin(saved);
          return;
        }
      }catch{}
      try{restore.hidden=!(await native('login.restoreAvailable'));}catch{}
      if(!restore.hidden&&isMac)restore.textContent='解鎖已儲存的登入資料';
    }
    void loadLoginForm();
    restore.onclick=async()=>{
      restore.disabled=true;
      try{
        const login=await native('login.restoreSaved');
        if(!login)throw new Error('暫時無法讀取已儲存的登入資料。');
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
      e.preventDefault();if(mountSaving)return;if(busy){showAppMessage('已有任務正在執行，請等待完成。');return;}if(!$('#mount-form').reportValidity())return;
      const mappings=$$('#mount-list .mount-pair').map(row=>({local:row.querySelector('[data-path="local"]').value.trim(),remote:row.querySelector('[data-path="remote"]').value.trim()}));
      mountSaving=true;busy=true;const form=$('#mount-form'),button=form.querySelector('[type="submit"]');const controls=[...form.querySelectorAll('button,input'),$('#close-mount')].map(control=>[control,control.disabled]);
      for(const [control] of controls)control.disabled=true;form.setAttribute('aria-busy','true');button.textContent='正在掛載…';mountProgress.hidden=false;mountProgressText.textContent=mappings.length?'正在建立檔案通道並檢查目錄…':'正在同步並卸載目錄…';mountElapsed.textContent='已用時 0 秒';
      const started=Date.now();const timer=setInterval(()=>{mountElapsed.textContent='已用時 '+Math.floor((Date.now()-started)/1000)+' 秒';},1000);renderTasks();
      try{const result=await native('mount.apply',{mappings});mountPairs=mappings;$('#mount-summary').textContent=mappings.length?'已掛載 '+mappings.length+' 組目錄。':'已卸載所有目錄。';$('#mount-dialog').close();if(result?.cloudWarning)showAppMessage(result.cloudWarning);}catch(error){showAppMessage(error.message);}
      finally{clearInterval(timer);mountSaving=false;busy=false;mountProgress.hidden=true;form.removeAttribute('aria-busy');for(const [control,disabled] of controls)control.disabled=disabled;button.textContent='儲存設定';renderTasks();}
    };
    function renderGatewayBadge(id,name,enabled){const badge=$('#'+id);badge.textContent=name+'網關'+(enabled?'已啟用':'未啟用');badge.classList.toggle('inactive',!enabled);}
    const openingClients=new WeakSet();
    const codexWarning='第三方功能，非本人創作，未經本人測試。使用請自行承擔後果。';
    function setLaunchAvailability(id,ready){
      const button=$('#'+id);button.disabled=!ready||openingClients.has(button);
    }
    function renderClaudeBridge(value){
      if(isMac){$('#bridge-state').textContent='暫不可用';$('#bridge-summary').textContent='當前附件僅有 Windows 適配器，macOS 暫不可用。';$('#bridge-manage').disabled=busy;$('#bridge-start').disabled=true;setLaunchAvailability('codex-launch',false);return;}
      if(value)bridgeStatus=value;const running=!!bridgeStatus?.running,ready=running&&!!bridgeStatus?.configuration;
      setLaunchAvailability('codex-launch',running&&!!bridgeStatus?.connected&&!bridgeBusy);
      renderGatewayBadge('codex-gateway-status','Codex',running&&!!bridgeStatus?.connected);
      $('#bridge-state').textContent=running?(bridgeStatus.connected?'已連線':'連線中'):bridgeStatus?.cleanupPending?'清理待重試':'未啟動';
      $('#bridge-summary').textContent=running?(bridgeStatus.message||'本機適配器執行中'):'習慣 Codex 客戶端？在裏面連接伺服器上的 Claude。';
      $('#bridge-manage').disabled=bridgeBusy||busy||!nativeServer?.statuses?.account;$('#bridge-close').disabled=bridgeBusy;
      for(const id of ['bridge-model','bridge-port','bridge-write','bridge-consent'])$('#'+id).disabled=bridgeBusy||running;
      if(running)$('#bridge-consent').checked=true;
      $('#bridge-start').hidden=running;$('#bridge-start').disabled=bridgeBusy||!$('#bridge-consent').checked;$('#bridge-start').textContent=bridgeBusy?'正在部署…':'部署並啟動';
      $('#bridge-stop').hidden=!running&&!bridgeStatus?.cleanupPending;$('#bridge-stop').disabled=bridgeBusy;$('#bridge-stop').textContent=bridgeStatus?.cleanupPending&&!running?'重試清理':'停止適配器';
      $('#bridge-config-area').hidden=!ready;$('#bridge-config').value=bridgeStatus?.configuration||'';$('#bridge-url').textContent='http://127.0.0.1:'+(bridgeStatus?.port||18797)+'/v1';
      $('#bridge-copy-config').disabled=!ready||bridgeBusy;$('#bridge-copy-key').disabled=!ready||bridgeBusy;$('#bridge-dialog-state').textContent=$('#bridge-state').textContent;
    }
    window.safeCodeClaudeBridgeStatus=renderClaudeBridge;
    async function pollClaudeBridge(){if(bridgePolling||bridgeBusy||!nativeServer)return;bridgePolling=true;try{renderClaudeBridge(await native('bridge.status'));}catch{}finally{bridgePolling=false;}}
    $('#bridge-manage').onclick=async()=>{
      if(isMac){
        await showAppMessage(codexWarning+'\n\n當前適配器僅支援 Windows，macOS 暫不可用。',{title:'第三方功能警告'});
        return;
      }
      if(!await showAppMessage(codexWarning,{title:'第三方功能警告',confirm:'繼續設定',cancel:'取消'}))return;
      await pollClaudeBridge();$('#bridge-consent').checked=false;$('#bridge-model').value=bridgeStatus?.model||'sonnet';$('#bridge-port').value=bridgeStatus?.port||18797;$('#bridge-write').checked=!!bridgeStatus?.writable;
      $('#bridge-progress').textContent=bridgeStatus?.running?bridgeStatus.message:'配置與現有供應商分開儲存。啟動只檢查接口和 SSH，不會自動傳送模型請求。';renderClaudeBridge();$('#bridge-dialog').showModal();
    };
    $('#bridge-close').onclick=()=>{if(!bridgeBusy)$('#bridge-dialog').close();};$('#bridge-dialog').addEventListener('cancel',event=>{if(bridgeBusy)event.preventDefault();});$('#bridge-consent').onchange=()=>renderClaudeBridge();
    $('#bridge-form').onsubmit=async event=>{
      event.preventDefault();if(bridgeBusy||!$('#bridge-consent').checked||!$('#bridge-form').reportValidity())return;
      bridgeBusy=true;renderClaudeBridge();$('#bridge-progress').textContent='正在校驗第三方組件、準備專用 SSH 授權並啟動本機接口…';
      try{bridgeStatus=await native('bridge.start',{port:Number($('#bridge-port').value),model:$('#bridge-model').value.trim().toLowerCase(),writable:$('#bridge-write').checked,thirdPartyAccepted:true});$('#bridge-progress').textContent=bridgeStatus.message+'。可複製配置與本機密鑰到 CC Switch；模型調用尚未自動測試。';}
      catch(error){$('#bridge-consent').checked=false;$('#bridge-progress').textContent=error.message;}
      finally{bridgeBusy=false;renderClaudeBridge();}
    };
    $('#bridge-stop').onclick=async()=>{
      if(bridgeBusy)return;bridgeBusy=true;renderClaudeBridge();$('#bridge-progress').textContent='正在關閉本機接口並撤銷專用授權，嘗試取消本機發起的遠端任務…';
      try{bridgeStatus=await native('bridge.stop');$('#bridge-consent').checked=false;$('#bridge-progress').textContent=bridgeStatus.cleanupPending?'本機接口已停止，遠端清理尚未確認，可恢復連線後重試。':'適配器已停止，專用授權已移除。';}
      catch(error){$('#bridge-progress').textContent=error.message;}
      finally{bridgeBusy=false;renderClaudeBridge();}
    };
    $('#bridge-copy-config').onclick=async()=>{try{await native('bridge.copy-config');$('#bridge-progress').textContent='供應商配置已複製，不包含密鑰。請在 CC Switch 中新增 Codex 供應商。';}catch(error){$('#bridge-progress').textContent=error.message;}};
    $('#bridge-copy-key').onclick=async()=>{try{await native('bridge.copy-key');$('#bridge-progress').textContent='本機適配器密鑰已複製。只填寫到本機 CC Switch/Codex，勿發給他人。';}catch(error){$('#bridge-progress').textContent=error.message;}};
    setInterval(()=>{if(!$('#workspace-screen').hidden)pollClaudeBridge();},4000);

    function connectionLabel(){
      if(!keyState.running)return keyState.configured?'待恢復':'未接入';
      if(keyState.deviceProblem)return '裝置回連待確認';
      if(keyState.requestError)return '接入異常';
      return keyState.clientConnected?'Claude 已連線':'網關已就緒';
    }
    function renderDesktopAccess(){
      const ready=connectionReady&&!!nativeServer?.statuses?.account;
      const checking=publicGatewayLoading&&!publicGatewayReachable&&!publicGatewayStatusError&&!publicGatewayError;
      const unavailable=!ready||busy||!!publicGatewayOperation;
      $('#claude-gateway-status').textContent=keyState.running?connectionLabel():'Claude 未接入';
      $('#claude-gateway-status').classList.toggle('inactive',!keyState.running||!!keyState.deviceProblem||!!keyState.requestError);
      $('#claude-desktop-state').textContent=connectionLabel();
      $('#claude-connection-reason').textContent=keyState.deviceProblem||keyState.requestError||'';
      setLaunchAvailability('claude-desktop-launch',keyState.running&&!keyBusy);
      $('#claude-desktop-manage').disabled=!ready;
      $('#public-gateway-runtime').disabled=!ready;
      $('#safe-code-api-enable').disabled=unavailable||checking||publicGatewayEnabled;
      $('#safe-code-api-enable').textContent=publicGatewayEnabled?'已啟用':'啟用';
      $('#safe-code-api-manage').disabled=unavailable||!publicGatewayEnabled;
      $('#safe-code-api-repair').disabled=unavailable||publicGatewayMissing;
      $('#safe-code-cloudcli-config').hidden=!publicGatewayNeedsCloudKey;
      $('#safe-code-cloudcli-api-key').disabled=!!publicGatewayOperation;
      $('#claude-desktop-dialog-state').textContent=publicGatewayOperation?'處理中…':checking?'正在讀取…':
        !publicGatewayReachable?'狀態待確認':publicGatewayEnabled?'公網已啟用':publicGatewayUpgrade?'待升級':
        publicGatewayMissing?'待安裝':'未啟用';
      $('#safe-code-api-progress').textContent=gatewayInstructions(ready,checking);
    }
    function gatewayInstructions(ready,checking){
      if(publicGatewayOperation)return publicGatewayOperation+' 可以關閉此視窗，稍後從「網關設定」查看。';
      if(!ready)return '伺服器連線尚未就緒。請關閉視窗，重新連接伺服器。';
      if(busy)return '伺服器正在執行其他任務。請關閉視窗，任務完成後再開啟「網關設定」。';
      if(checking)return '正在讀取公網網關狀態。可以關閉此視窗。';
      if(publicGatewayError){
        if(publicGatewayError.action==='manage')return publicGatewayError.message+
          (publicGatewayEnabled?' 點擊「管理網關」重試。':' 點擊「啟用」重新檢查公網網關。');
        return publicGatewayError.message;
      }
      if(!publicGatewayReachable&&publicGatewayStatusError)return publicGatewayStatusError+
        (publicGatewayEnabled?' 點擊「管理網關」重試，也可以關閉視窗後檢查 SSH 連線。':' 檢查 SSH 連線後，點擊「啟用」重試。');
      if(publicGatewayNeedsCloudKey)return '填寫下方的 CloudCLI API Key，再點擊「啟用」。';
      if(publicGatewayEnabled)return '點擊「管理網關」建立 API Key，再回到主介面貼上。';
      if(publicGatewayUpgrade)return '點擊「啟用」升級公網網關。已有密鑰可繼續使用。';
      if(publicGatewayMissing)return '點擊「啟用」安裝並開放公網網關。首次安裝可能需要幾分鐘。';
      return '點擊「啟用」開放公網網關。';
    }
    function setPublicGatewayOperation(message){
      publicGatewayOperation=message;
      renderDesktopAccess();
    }
    function readPublicGatewayStatus(){
      return native('safeCodeApi.publicStatus',{},{
        ms:12000,message:'讀取公網網關狀態超時。'
      });
    }
    function renderPublicGatewayStatus(status,error=''){
      const footer=$('#public-gateway-footer'),badge=$('#public-gateway-runtime'),quota=$('#footer-claude-limits');
      footer.hidden=!connectionReady||!nativeServer?.statuses?.account;
      const reachable=Boolean(status);
      if(reachable) {
        publicGatewayMissing=status.installed===false;
        publicGatewayUpgrade=!publicGatewayMissing&&(status.backend!=='cloudcli'||(status.gatewayRevision??0)<requiredGatewayRevision);
        publicGatewayEnabled=status.enabled===true&&!publicGatewayUpgrade;
        if(status.cloudcliConfigured===true||publicGatewayEnabled)publicGatewayNeedsCloudKey=false;
        if(publicGatewayEnabled&&publicGatewayError?.action==='enable')publicGatewayError=undefined;
      }
      publicGatewayReachable=reachable;
      publicGatewayStatusError=error;
      badge.classList.toggle('inactive',!reachable||!publicGatewayEnabled);
      badge.title=error||(publicGatewayEnabled?'開啟 Safe Code API 網關管理':'開啟 Safe Code API 公網設定');
      if(!reachable)badge.textContent=/SSH 認證失敗|伺服器身份已變化/.test(error||'')
        ?'公網網關 · 需檢查 SSH':publicGatewayEnabled?'公網網關 · 管理連線重連中':'公網網關 · 狀態不可用';
      else if(publicGatewayMissing)badge.textContent='公網網關 · 待安裝';
      else if(publicGatewayUpgrade)badge.textContent='公網網關 · 待升級';
      else if(!publicGatewayEnabled)badge.textContent='公網網關 · 未啟用';
      else if(status.waiting==null)badge.textContent='公網網關 · 已啟用';
      else {
        const limit=String(status.concurrency_text??status.concurrency);
        badge.textContent='公網網關 · 併發 '+status.active+(limit==='0'?'（不限）':'/'+limit)+' · 排隊 '+status.waiting;
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
      if(attempt!==connectionAttempt)throw new Error('伺服器連線已變更，請重新查詢。');
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
      void showAppMessage('已建立 VPS 上的 CloudCLI 網頁帳戶，請妥善儲存。\n\n帳號 '+username+'\n密碼 '+password,
        {title:'CloudCLI 帳戶已建立',confirm:'已儲存',clearOnClose:true});
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
      if(!publicGatewayCreatedAccount&&!$('#app-message-dialog').open&&publicGatewayEnabled)
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
      repairStatus.textContent='重新安裝期間服務會短暫中斷，建議等正在執行的請求完成。';
      repairDialog.showModal();
    };
    for(const button of repairDialog.querySelectorAll('[data-close-gateway-repair]')){
      button.onclick=()=>{if(!repairing)repairDialog.close();};
    }
    repairDialog.addEventListener('cancel',event=>{if(repairing)event.preventDefault();});
    $('#gateway-repair-form').onsubmit=async event=>{
      event.preventDefault();
      if(repairing||repairStart.hidden||!connectionReady||publicGatewayOperation)return;
      const attempt=connectionAttempt, reinstallCloudCLI=repairCloud.checked;
      setRepairBusy(true);
      setPublicGatewayOperation('正在修復網關。');
      repairStatus.textContent=reinstallCloudCLI?'正在重新安裝網關和 CloudCLI，下載安裝可能需要幾分鐘。':'正在重新安裝網關並檢查服務。';
      try{
        await publicGatewayTask?.promise;
        if(attempt!==connectionAttempt)return;
        const status=await native('safeCodeApi.repair',{accepted:true,reinstallCloudCLI});
        if(attempt!==connectionAttempt)return;
        publicGatewayError=undefined;
        renderPublicGatewayStatus(status);
        repairStatus.textContent=status.cloudcliReinstalled?'網關和 CloudCLI 已重新安裝，服務檢查通過。':'網關已重新安裝，服務檢查通過。';
        repairStart.hidden=true;
        repairBack.textContent='完成';
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
      if($('#safe-code-api-enable').disabled)return;
      const attempt=connectionAttempt, keyField=$('#safe-code-cloudcli-api-key');
      const apiKey=publicGatewayNeedsCloudKey?keyField.value.trim():'';
      if(publicGatewayNeedsCloudKey&&!apiKey){
        publicGatewayError={message:'請填寫 CloudCLI API Key。'};renderDesktopAccess();keyField.focus();return;
      }
      publicGatewayError=undefined;
      let needsCloudKey=false;
      setPublicGatewayOperation('正在確認伺服器上的網關狀態。');
      try{
        await publicGatewayTask?.promise;
        if(attempt!==connectionAttempt)return;
        if(!publicGatewayReachable){
          const status=await readPublicGatewayStatus();
          if(attempt!==connectionAttempt)return;
          renderPublicGatewayStatus(status);
        }
        if(publicGatewayMissing||publicGatewayUpgrade){
          keyField.value='';
          setPublicGatewayOperation('正在準備 CloudCLI 並部署公網網關，可能需要幾分鐘。');
          const status=await native('safeCodeApi.install',apiKey?{accepted:true,cloudcli:{apiKey}}:{accepted:true},{
            ms:13*60000,message:'等待網關部署結果超時，伺服器可能仍在處理。'
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
        if(!publicGatewayEnabled){
          setPublicGatewayOperation('正在啟用公網網關。');
          const status=await native('safeCodeApi.enablePublic',{accepted:true},{
            ms:35000,message:'等待公網網關啟用結果超時。'
          });
          if(attempt!==connectionAttempt)return;
          renderPublicGatewayStatus(status);
        }
        if(!publicGatewayEnabled)publicGatewayError={action:'enable',message:'未能確認公網已啟用，點擊「啟用」重試。'};
      }catch(error){
        if(attempt===connectionAttempt){
          publicGatewayReachable=false;
          publicGatewayError={action:'enable',message:error.message+' 檢查連線後，點擊「啟用」重試。'};
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
      setPublicGatewayOperation('正在開啟網關管理。');
      try{
        await native('window.gateway.open',{},{
          ms:35000,message:'開啟網關管理超時。'
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
      $('#api-duration').textContent=hours?`${hours} 小時 ${minutes} 分`:minutes?`${minutes} 分 ${seconds} 秒`:`${seconds} 秒`;
    }
    function renderApi(){
      $('#api-state-label').textContent=connectionLabel();
      $('#api-state').classList.toggle('connected',!!keyState.running&&!keyState.deviceProblem&&!keyState.requestError);
      $('#api-device').textContent=[keyState.deviceAlias,keyState.deviceProblem||keyState.requestError].filter(Boolean).join(' · ');
      $('#api-manage-key').textContent=keyState.running?'接入設定':'恢復接入';
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
        $('#api-balance').textContent=data.remainingNanos===null?'不限額':money(data.remainingNanos);
        $('#api-spend').textContent=money(data.usedNanos);
        const rows=Array.isArray(data.models)?data.models:[];
        const elements=rows.map(model=>{
          const row=document.createElement('tr');
          const count=model.input+model.output+model.cacheRead+model.cacheWrite;
          const fee=model.unpricedTokens>0?(model.costNanos?money(model.costNanos)+' + 未計價':'未計價'):money(model.costNanos);
          for(const [index,value] of [model.model,tokens.format(count),fee].entries()){
            const cell=document.createElement(index===0?'th':'td');
            if(index===0)cell.scope='row';
            cell.textContent=value;row.append(cell);
          }
          return row;
        });
        $('#api-models').replaceChildren(...elements);
        apiTable.hidden=!rows.length;apiUsage.hidden=!!rows.length;
        apiUsage.textContent=Array.isArray(data.models)?'當前 Key 暫無模型用量。':'當前網關版本暫不提供模型明細。';
      }catch{
        if(request!==apiRequest)return;
        apiUsage.hidden=false;
        apiUsage.textContent=apiTable.hidden?'暫時無法獲取用量，請點擊「重新整理用量」重試。':'重新整理失敗，已保留上次查詢的數據。';
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
      resetApiUsage(keyState.running?'正在查詢用量…':'恢復接入後即可查看用量。');
      renderApi();void refreshApiUsage();
    }
    function closeApiAccess(){
      rememberApiMode(false);
      resetApiUsage('正在查詢用量…');
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
      label.labels[0].textContent=entry?'編輯備註':'名稱';
      label.value=entry?.name??'';
      keyForm.querySelector('[type=submit]').textContent=entry?'儲存備註':'儲存 Key';
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
      if(!rows.length)keyMessage.textContent='尚未儲存 Key。';
      for(const entry of rows){
        const row=document.createElement('div'), info=document.createElement('div');
        row.className='key-row'+(entry.running?' current':'');
        const name=document.createElement('strong'), balance=document.createElement('small');
        const heading=document.createElement('div'), edit=document.createElement('button');
        heading.className='key-row-name';edit.className='text-button';edit.type='button';
        name.textContent=entry.name;edit.textContent='編輯備註';
        edit.setAttribute('aria-label','編輯備註：'+entry.name);
        edit.onclick=()=>editKeyForm(entry);
        heading.append(name,edit);balance.textContent='餘額查詢中…';info.append(heading,balance);
        const action=document.createElement(entry.running?'span':'button');
        action.className=entry.running?'key-current':'key-secondary';
        action.textContent=entry.running?'正在使用':'切換';
        if(!entry.running){
          action.type='button';action.setAttribute('aria-label','切換到 '+entry.name);
          action.onclick=()=>switchSavedKey(entry);
        }
        const actions=document.createElement('div');actions.className='key-row-actions';
        if(!entry.current){
          const forget=document.createElement('button');
          forget.type='button';forget.className='text-button';forget.textContent='移除';
          forget.setAttribute('aria-label','移除 '+entry.name);
          forget.onclick=async()=>{
            if(libraryBusy||!await showAppMessage('從本機移除「'+entry.name+'」？',{confirm:'移除',cancel:'取消'}))return;
            lockLibrary(true);
            try{await native('desktopKey.forget',{id:entry.id});await loadKeyList();}
            catch(error){keyMessage.textContent=error.message;}
            finally{lockLibrary(false);}
          };
          actions.append(forget);
        }
        actions.append(action);row.append(info,actions);keyList.append(row);
        native('desktopKey.savedBalance',{id:entry.id}).then(data=>{
          balance.textContent=data.remainingNanos===null?'不限額':'餘額 '+money(data.remainingNanos);
        }).catch(()=>{balance.textContent='餘額暫不可用';});
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
      catch(error){await showAppMessage('已切換 Key。'+error.message);}
    }
    async function refreshSwitchState(){
      keyState=await native('desktopKey.status');
      if(!keyState.running)resetApiUsage('選擇已儲存的 Key 恢復接入。');
      renderKey();
      await loadKeyList();
    }
    async function switchSavedKey(entry){
      if(libraryBusy)return;
      lockLibrary(true);keyMessage.textContent='正在連接 '+entry.name+'…';
      try{
        const result=await native('desktopKey.enable',{savedId:entry.id,replace:true});
        if(result.requiresDevice){
          switchPending=true;keyDeviceSetup=result;openHostControl();return;
        }
        keyState=result;
        if(!keyState.running)throw new Error(keyState.error||'接入未啟動。');
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
      const target=keyState.deviceAlias?'\n工具目標：已授權的本機裝置 '+keyState.deviceAlias:'';
      if(keyState.unbindPending)return keyState.error;
      if(keyState.error)return (keyState.configured?'已儲存 Key，暫未恢復接入。\n':'')+keyState.error;
      if(keyState.running)return connectionLabel()+'。'+(keyState.deviceProblem||keyState.requestError||'')+target+'\n'+(args?.replace||args?.useSaved?'請完全退出並重新開啟 Claude Desktop 以應用更改。':'首次接入請完全退出並重新開啟 Claude Desktop。');
      if(keyState.configured)return '已儲存 Key，當前 Claude 配置未啟用此接入。';
      return op==='disable'?'已停用。\n請重新開啟 Claude Desktop 以應用更改。':'尚未啟用。';
    }
    function renderKey(){
      const configured=keyState.configured;
      const editing=!configured||keyReplacing;
      $('#desktop-key-field').hidden=!editing;
      keyInput.disabled=keyBusy||!editing;
      keyEnable.hidden=!editing;
      keyEnable.disabled=keyBusy;
      keyEnable.textContent=keyReplacing?'驗證並更換':'驗證並啟用';
      keyReplace.hidden=!configured||keyState.unbindPending;
      keyReplace.disabled=keyBusy;
      keyReplace.textContent=keyReplacing?'取消更換':'更換 Key';
      keyResume.hidden=!configured||keyState.running||keyReplacing||keyState.unbindPending;
      keyResume.disabled=keyBusy;
      $('#desktop-key-refresh').hidden=!keyState.running||keyReplacing;
      $('#desktop-key-refresh').disabled=keyBusy;
      $('#desktop-key-disable').hidden=!configured||keyReplacing;
      $('#desktop-key-disable').disabled=keyBusy;
      $('#desktop-key-disable').textContent=keyState.unbindPending?'重試解綁':'停用接入';
      $('#desktop-key-open').hidden=!keyState.running||keyReplacing;
      setLaunchAvailability('desktop-key-open',keyState.running&&!keyBusy&&!keyReplacing);
      $('#desktop-key-close').disabled=keyBusy;
      renderDesktopAccess();
      if(!apiScreen.hidden)renderApi();
    }
    async function keyAction(op,args={}){
      if(keyBusy)return;
      keyBusy=true;
      if(op==='enable')keyProgress.textContent=args.useSaved?'正在重新啟用已儲存的 Key…':'正在驗證 Key 並獲取模型…';
      else if(op==='refreshModels')keyProgress.textContent='正在更新模型列表…';
      else keyProgress.textContent=op==='disable'?'正在停用接入…':'正在讀取接入狀態…';
      renderKey();
      try{
        const result=await native('desktopKey.'+op,args);
        if(result?.requiresDevice){
          keyProgress.textContent='網關已驗證，請確認本機遠端控制。';
          keyDeviceSetup=result;
          openHostControl();
          return;
        }
        keyState=result;
        if(op==='enable'&&keyState.running)keyReplacing=false;
        keyProgress.textContent=op==='refreshModels'?'模型列表已更新。請完全退出並重新開啟 Claude Desktop。':describeKey(op,args);
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
      keyProgress.textContent=keyReplacing?'貼上新的接入 Key，驗證通過後切換。':describeKey();
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
    $('#claude-desktop-launch').onclick=()=>openLocalClient($('#claude-desktop-launch'),'claudeDesktop.open',renderDesktopAccess);
    $('#codex-launch').onclick=()=>openLocalClient($('#codex-launch'),'codex.open',renderClaudeBridge);
    const hostDialog=$('#host-dialog');
    const hostVerified=status=>status?.enabled===true&&status.state==='connected';
    const isHostSetupCurrent=attempt=>attempt===connectionAttempt&&connectionReady&&!!nativeServer?.statuses?.account;
    function renderKeyDeviceSetup(){
      $('#host-dialog-state').textContent='等待授權';
      $('#host-close').disabled=hostBusy;
      $('#host-risk').disabled=hostBusy;
      $('#host-alias').disabled=true;
      $('#host-enable').hidden=false;
      $('#host-enable').disabled=hostBusy||!$('#host-risk').checked;
      $('#host-enable').textContent=keyDeviceSetup.existingControl
        ? (hostBusy?'正在驗證…':'同意並使用')
        : (hostBusy?'正在部署…':'同意並開始部署');
      $('#host-disable').hidden=true;
      $('#host-command-area').hidden=true;
    }
    function renderHostControl(value){
      if(keyDeviceSetup){renderKeyDeviceSetup();return;}
      if(value)hostStatus=value;
      const enabled=!!hostStatus?.enabled,connected=hostVerified(hostStatus);
      $('#host-state').textContent=enabled?(connected?'已連線':hostStatus?.state==='attention'?'需要處理':'重連中'):'未啟用';
      $('#host-summary').textContent=enabled?(hostStatus.message||'本機控制已啟用'):'讓伺服器上的 AI 連接本機命令列，操作本機檔案和程式。';
      $('#host-manage').disabled=hostBusy||busy||!nativeServer?.statuses?.account;
      if(enabled)$('#host-risk').checked=true;
      $('#host-close').disabled=hostBusy;$('#host-alias').disabled=hostBusy||enabled;$('#host-risk').disabled=hostBusy||enabled;
      $('#host-enable').hidden=enabled;$('#host-enable').disabled=hostBusy||!$('#host-risk').checked;$('#host-enable').textContent=hostBusy?'正在部署…':'同意並開始部署';
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
        if(typeof status?.enabled!=='boolean')throw new Error('無法確認本機遠端控制狀態，請重新檢查伺服器連線。');
        renderHostControl(status);
        return status;
      }finally{if(hostStatusTask===task)hostStatusTask=null;}
    }
    function openHostControl(){
      $('#host-risk').checked=false;
      $('#host-alias').value=keyDeviceSetup
        ? (keyDeviceSetup.alias??'由網關分配')
        : (hostStatus?.alias||(isMac?'mac':'win'));
      const instructions=hostStatus?.enabled?hostStatus.message:(isMac
        ?'請先在系統設定 → 通用 → 共享開啟遠端登入，僅允許當前帳戶。Safe Code 將添加專用密鑰，不會開啟或更改系統 SSH 服務。'
        :'啟用時會請求 Windows 管理員授權；首次部署需要幾秒至幾十秒。');
      $('#host-progress').textContent=keyDeviceSetup
        ? (keyDeviceSetup.reuseControl
          ? (keyDeviceSetup.existingControl
            ? '這把 Key 由當前 VPS 簽發。授權後將使用已驗證的本機遠端控制通道。'
            : '這把 Key 由當前 VPS 簽發。授權後將透過當前 SSH 會話開啟本機遠端控制。')
          : (isMac
            ? '將在 Key 所屬的網關建立加密通道，併為此裝置分配唯一連線別名。SafeCode 只會為當前 macOS 帳戶添加一把專用 SSH 公鑰。'
            : '將在 Key 所屬的網關建立加密通道，併為此裝置分配唯一連線別名。部署需要 Windows 管理員授權。'))
        : instructions;
      if(keyDeviceSetup)$('#host-progress').textContent+=' 下次在這台電腦選擇同一把 Key 時，會使用本次授權重新連線。';
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
        if(switchPending){switchPending=false;keyMessage.textContent='已取消切換。';}
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
            ? '正在檢查已授權的本機控制通道並啟用 Key…'
            : (isMac?'正在為系統遠端登入添加專用公鑰，接下來會驗證本機遠端控制並啟用 Key。':'請完成 Windows 管理員授權。接下來會驗證本機遠端控制並啟用 Key。'))
          : (isMac?'正在為系統遠端登入添加專用公鑰，並建立 FRP 通道驗證 SSH 登入。':'請完成 Windows 管理員授權。接下來會建立 FRP 通道並驗證 SSH 登入。');
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
      hostBusy=true;hostChange++;renderHostControl();$('#host-progress').textContent=isMac?'正在為系統遠端登入添加專用公鑰，並驗證 macOS 帳戶身份…':'正在準備專用密鑰與本機 SSH，請完成 Windows 管理員授權。隨後會驗證伺服器能否登入本機…';
      try{
        const status=await native('host.enable',{alias:$('#host-alias').value.trim(),riskAccepted:true});
        if(!isHostSetupCurrent(attempt))return;
        renderHostControl(status);
        $('#host-progress').textContent=hostVerified(status)?'部署完成，已通過實際 SSH 登入驗證。複製下方命令發給伺服器上的 AI。':(status?.message||'正在驗證本機遠端控制。');
      }
      catch(error){if(attempt===connectionAttempt){$('#host-risk').checked=false;$('#host-progress').textContent=error.message;}}
      finally{hostBusy=false;renderHostControl();}
    };
    $('#host-disable').onclick=async()=>{
      if(hostBusy)return;const attempt=connectionAttempt;
      hostBusy=true;hostChange++;renderHostControl();$('#host-progress').textContent=isMac?'正在斷開專用通道並撤銷密鑰；系統遠端登入設定保持不變…':'正在關閉控制通道並停止本機 SSH…';
      try{
        const status=await native('host.disable');
        if(attempt!==connectionAttempt)return;
        renderHostControl(status);$('#host-risk').checked=false;$('#host-progress').textContent='本機遠端控制已停用。';
      }
      catch(error){if(attempt===connectionAttempt)$('#host-progress').textContent=error.message;}
      finally{hostBusy=false;renderHostControl();}
    };
    $('#host-copy').onclick=async()=>{if(!hostStatus?.command)return;try{await native('copy',{text:hostStatus.command});$('#host-progress').textContent='命令已複製。請發給當前伺服器上的 AI。';}catch(error){$('#host-progress').textContent=error.message;}};
    setInterval(()=>{if(!$('#workspace-screen').hidden)pollHostControl();},2000);
    let ipSaving=false;
    const ipProgress=document.createElement('div');ipProgress.className='connection-progress';ipProgress.hidden=true;ipProgress.setAttribute('role','status');ipProgress.setAttribute('aria-live','polite');
    const ipSpinner=document.createElement('span');ipSpinner.className='connection-spinner';ipSpinner.setAttribute('aria-hidden','true');const ipProgressText=document.createElement('span'),ipElapsed=document.createElement('span');ipProgress.append(ipSpinner,ipProgressText,ipElapsed);$('#ip-policy-form .config-footer').before(ipProgress);
    $('#ip-policy-dialog').addEventListener('cancel',event=>{if(ipSaving)event.preventDefault();});
    const originalOpenIpPolicy=openIpPolicy;openIpPolicy=function(){
      if(busy){showAppMessage('已有任務正在執行，請等待完成。');return;}
      if(nativeServer?.socksEgress?.enabled){showAppMessage('請先在「開發模式」停用 SOCKS5 出口，再更換 IP。');return;}
      originalOpenIpPolicy();
      $('#ingress-ip-input').value=ipPolicy.ingressIp||serverConnection.ingressIp;
      const candidates=(nativeServer?.assignedIps||[]).filter(isPublicIpv4);
      $('#egress-ip-input').value=ipPolicy.egressIp||(candidates.length===2?candidates.find(ip=>ip!==serverConnection.ingressIp):serverConnection.egressIp)||'';
      syncIpPickers();$('#ip-policy-hint').textContent='只使用伺服器已綁定的 IP。儲存時自動檢查入口和真實出口；無法驗證會恢復原設定。';
    };
    $('#ip-policy-form').onsubmit=async e=>{
      e.preventDefault();if(ipSaving)return;syncIpPickers();closeIpPickers();if(busy){showAppMessage('已有任務正在執行，請等待完成。');return;}if(!$('#ip-policy-form').reportValidity())return;
      const args={ingressIp:$('#ingress-ip-input').value.trim(),egressIp:$('#egress-ip-input').value.trim(),lockIngress:$('#lock-ingress').checked,lockEgress:$('#lock-egress').checked};
      for(const [key,id] of [['ingressIp','ingress-ip-input'],['egressIp','egress-ip-input']]){
        if(!isPublicIpv4(args[key])||!nativeServer?.assignedIps?.includes(args[key])){$('#ip-policy-hint').textContent='未檢測到有效的已綁定 IP，請重新連接伺服器後再選擇。';return;}
      }
      ipSaving=true;busy=true;const form=$('#ip-policy-form'),button=form.querySelector('[type=submit]');const controls=[...form.querySelectorAll('button,input,select'),$('#close-ip-policy')].map(control=>[control,control.disabled]);
      for(const [control] of controls)control.disabled=true;form.setAttribute('aria-busy','true');button.textContent='正在驗證…';ipProgress.hidden=false;ipProgressText.textContent='正在檢查 IP 和路由…';ipElapsed.textContent='已用時 0 秒';
      const started=Date.now();const timer=setInterval(()=>{ipElapsed.textContent='已用時 '+Math.floor((Date.now()-started)/1000)+' 秒';},1000);renderTasks();
      try{
        const result=await native('ip.apply',args);
        const ingressIp=result.ipPolicy?.ingressIp||args.ingressIp;
        if(ingressIp!==serverConnection.ingressIp){
          const [saved,draft]=await Promise.all([native('deployment.settings.get').catch(()=>null),native('account.draft.get').catch(()=>null)]);
          applyDeploymentSettings(saved);accountOptions=draft||{};accountDraftPending=!!draft;
        }
        applyServer(result);serverConnection={ingressIp,egressIp:result.observedEgressIp||''};$('#server-ingress').textContent=serverConnection.ingressIp;$('#server-address').textContent=serverConnection.egressIp||'待確認';
        $('#ip-policy-dialog').close();void queryIpInfo(serverConnection.egressIp);
      }catch(error){$('#ip-policy-hint').textContent='設定未完成，填寫內容已保留。請查看錯誤，確認當前狀態後重試。';showAppMessage(error.message);}
      finally{clearInterval(timer);ipSaving=false;busy=false;ipProgress.hidden=true;form.removeAttribute('aria-busy');for(const [control,disabled] of controls)control.disabled=disabled;button.textContent='儲存設定';renderTasks();}
    };
    const originalNativeLog=window.safeCodeNativeLog;window.safeCodeNativeLog=function(step,line){if(ipSaving&&step==='ip_policy')ipProgressText.textContent=line;originalNativeLog?.(step,line);};

    const socksDialog=$('#socks-dialog'), socksUrl=$('#socks-url'), socksConfirm=$('#socks-confirm');
    let socksBusy=false, socksReading=false, socksResult=null;
    function updateSocksCard(status){
      if(!status?.enabled)return;
      $('#ip-policy-state').textContent=status.guardActive?'SOCKS5 開發模式':'出口規則異常';
      $('#ip-policy-summary').textContent=status.guardActive
        ?'機房入口保留，SOCKS5 出站。'+(status.tunnelRunning?'':'隧道暫停，出站已阻斷。')
        :'SOCKS5 防火牆規則未生效，請檢查伺服器。';
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
        setSocksResult(protocol,enabled?(status.observedIp?'上次驗證通過':'待驗證'):passed===undefined?'待檢測':passed?'通過':'未通過',
          !enabled&&passed!==undefined?(passed?'pass':'fail'):'');
      }
      setSocksResult('observed',(enabled?status.observedIp:socksResult?.observedIp)||'待檢測');
    }
    function socksStatusText(){
      if(!connectionReady||developerPreview)return '連接 Ubuntu 伺服器後可檢測和啟用。';
      const status=nativeServer?.socksEgress;
      if(!status?.enabled)return '檢測通過後可以啟用。首次啟用會下載隧道組件。';
      if(!status.guardActive)return '阻斷規則未生效，請檢查伺服器。';
      return status.endpoint+' · '+(status.issue||(status.tunnelRunning?'已啟用，代理不可用時阻斷出站。':'隧道未執行，出站已阻斷。'));
    }
    $('#socks-experimental').onclick=async()=>{
      if(busy){showAppMessage('已有任務正在執行，請等待完成。');return;}
      socksResult=null;
      socksConfirm.checked=false;
      socksUrl.value='';
      const server=nativeServer;
      socksReading=!!(connectionReady&&server&&!developerPreview);
      $('#socks-status').textContent=socksReading?'正在讀取伺服器出口狀態…':socksStatusText();
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
    $('#socks-test').onclick=()=>runSocksOperation('正在從伺服器檢測 TCP 和 UDP 轉發…',async()=>{
      socksResult=null;socksConfirm.checked=false;
      socksResult=await native('socks.probe',{proxyUrl:socksUrl.value.trim()});
      const failures=[socksResult.tcpMessage&&'TCP '+socksResult.tcpMessage,socksResult.udpMessage&&'UDP '+socksResult.udpMessage].filter(Boolean);
      $('#socks-status').textContent=failures.join(' ')||'檢測通過，可以啟用。';
    });
    async function changeSocks(op,args){
      const server=await native(op,args);
      socksUrl.value='';socksConfirm.checked=false;socksResult=null;
      applyServer(server);
      serverConnection.egressIp=server.observedEgressIp||'';
      $('#server-address').textContent=serverConnection.egressIp||'待確認';
      $('#socks-status').textContent=socksStatusText();
      if(serverConnection.egressIp)void queryIpInfo(serverConnection.egressIp);
    }
    $('#socks-form').onsubmit=event=>{
      event.preventDefault();
      if(!socksResult?.tcp||!socksResult?.udp||!socksConfirm.checked)return;
      return runSocksOperation('正在部署隧道並驗證整機出站…',()=>changeSocks('socks.enable',{proxyUrl:socksUrl.value.trim(),accepted:true}));
    };
    $('#socks-disable').onclick=()=>runSocksOperation('正在停用代理並恢復原出口…',()=>changeSocks('socks.disable',{}));
    // 驗收入口僅在桌面啟動參數指定了私密設定檔時由宿主調用。
    window.safeCodeRunAcceptance=async config=>{
      try{
        await native('acceptance.capture',{tag:'notice'});if($('#accept').getBoundingClientRect().bottom>innerHeight||$('#exit').getBoundingClientRect().bottom>innerHeight)throw new Error('聲明按鈕超出視窗。');$('#username').value=config.username;$('#ip').value=config.host;originalShowScreen('login');await native('window.resize',{wide:false});await native('acceptance.capture',{tag:'login'});const connectBounds=$('#connect').getBoundingClientRect();const lowest=Math.max(connectBounds.bottom,advanced.getBoundingClientRect().bottom,$('#back').getBoundingClientRect().bottom);const loginLayout={buttonBottom:connectBounds.bottom,controlsBottom:lowest,viewportHeight:innerHeight,visible:lowest<=innerHeight};if(!loginLayout.visible)throw new Error('登入連線按鈕超出視窗。');const restored=await native('login.load');const restoredLogin=restored?.host===config.host&&restored?.username===config.username&&restored?.password===config.password;let result=await connect(config);config.password='';
        if(!deploymentSettingsReady())throw new Error('部署驗收需要先為該連線手動儲存工作用戶和本土化設定。');
        result=await native('deploy',{step:'ssh',username:'workspace'});applyServer(result);await metricsTask;
        $('#reveal').click();if($('#password').type!=='text'||!$('#reveal svg')||$('#reveal').textContent)throw new Error('密碼顯示按鈕異常');$('#reveal').click();
        const realIp=$('#server-ingress').textContent;$('#server-ingress').nextElementSibling.click();if($('#server-ingress').textContent===realIp)throw new Error('IP 隱藏失敗');$('#server-ingress').nextElementSibling.click();if($('#server-ingress').textContent!==realIp)throw new Error('IP 恢復失敗');
        const dirs=await native('folder.list');if(!dirs.path.endsWith('/workspace'))throw new Error('遠端目錄起始位置錯誤');await native('folder.list',{path:dirs.parent});
        $('#mount-list').replaceChildren();appendMountPair({local:'',remote:''});if(document.querySelectorAll('.folder-input button').length!==2)throw new Error('目錄選擇按鈕位置錯誤');
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
      }catch{showAppMessage('無法儲存聲明確認，請檢查本機存儲空間。');}
    };
    const startupReady=preferencesReady.then(()=>native('notice.state')).then(state=>{if(state.accepted&&(!window.__safeCodeAcceptance||window.__safeCodeConnectionAcceptance)){showScreen('login');confirmServices().then(showPreferences).catch(()=>{});}else{showScreen('notice');}}).catch(()=>{showScreen('notice');});
    startupReady.then(async()=>{
      if(window.__safeCodeAcceptance||window.__safeCodeConnectionAcceptance||!$('#notice-screen').hidden)return;
      await keyAction('status');
      const prefersApi=keyState.configured&&await native('desktopKey.mode').catch(()=>false);
      if(prefersApi&&!$('#login-screen').hidden){showApiAccess(false);return;}
      const settings=await native('gateways.resume-settings');
      if(settings&&!$('#login-screen').hidden&&!connectionInFlight)await connect(settings,true);
    }).catch(()=>{});
    for(const button of document.querySelectorAll('[data-open-services]')){
      if(isMac)button.onclick=()=>native('services.open').catch(error=>showAppMessage(error.message));
      else button.addEventListener('click',refreshPreferences);
    }
    // 版本查詢與統計授權相互獨立，驗收模式不調用外部服務。
    if(!window.__safeCodeAcceptance)setTimeout(()=>checkUpdate(false),1500);
    window.safeCodeRunIpAcceptance=async config=>{
      const checks={};
      try{
        await startupReady;await native('diagnostics.consent',{enabled:false});await native('services.consent',{enabled:false,confirmed:true});await native('notice.accept');
        $('#ip').value=config.host;$('#username').value=config.username;
        const connected=await connect(config);config.password='';if(!connected||connected.osId!=='ubuntu')throw new Error('未連線指定的真實測試目標');
        if(nativeServer.observedEgressIp!==config.egressIp||!nativeServer.ipPolicyHealthy)throw new Error('初始住宅出口基線不健康');checks.initialBaseline=true;
        let rejected=false;try{await native('ip.apply',{ingressIp:config.ingressIp,egressIp:'192.0.2.123',lockIngress:true,lockEgress:true});}catch{rejected=true;}
        if(!rejected)throw new Error('未分配出口沒有被拒絕');checks.unassignedRejected=true;
        switchMode('maintenance');openIpPolicy(true);
        $('#ingress-ip-input').value=config.egressIp;$('#egress-ip-input').value=config.egressIp;$('#lock-ingress').checked=true;$('#lock-egress').checked=true;
        const changed=$('#ip-policy-form').onsubmit({preventDefault(){}});if(!ipSaving||!$('#close-ip-policy').disabled)throw new Error('應用等待狀態或防重複提交未生效');
        await native('acceptance.capture',{tag:'ip'});await changed;
        if($('#ip-policy-dialog').open||nativeServer.ipPolicy?.ingressIp!==config.egressIp||nativeServer.observedEgressIp!==config.egressIp)throw new Error('更換入口未成功');checks.ingressChanged=true;
        const remembered=await native('login.load');if(remembered?.host!==config.egressIp)throw new Error('新入口沒有儲存到連線記憶');checks.connectionRemembered=true;
        openIpPolicy(true);$('#ingress-ip-input').value=config.ingressIp;$('#egress-ip-input').value=config.egressIp;$('#lock-ingress').checked=true;$('#lock-egress').checked=true;await $('#ip-policy-form').onsubmit({preventDefault(){}});
        if($('#ip-policy-dialog').open||nativeServer.ipPolicy?.ingressIp!==config.ingressIp||nativeServer.observedEgressIp!==config.egressIp||!nativeServer.ipPolicyHealthy)throw new Error('恢復極速入口與住宅出口失敗');checks.baselineRestored=true;
        const inspected=await native('inspect');applyServer(inspected);if(!inspected.ipPolicyHealthy)throw new Error('重新巡檢未讀到已儲存策略');checks.persistedReadback=true;
        await native('acceptance.capture',{tag:'maintenance'});
        await native('acceptance.done',{success:true,checks,ingress:nativeServer.ipPolicy.ingressIp,observedEgress:nativeServer.observedEgressIp,policyHealthy:nativeServer.ipPolicyHealthy});
      }catch(error){await native('acceptance.done',{success:false,checks,error:error.message});}
    };
    window.safeCodeRunUpdateAcceptance=async()=>{
      try{
        await startupReady;showScreen('login');await native('window.resize',{wide:false});
        displayUpdate({currentVersion:'Beta-0.4.3.3',version:'Beta-0.4.3.4',status:'發現新版本（介面預覽）',page:null,canInstall:false});
        const notice=$('#title-update-notice'),bounds=notice.getBoundingClientRect(),controls=$('.window-controls').getBoundingClientRect();
        if(notice.hidden||bounds.right>controls.left||bounds.width<60)throw new Error('標題更新提示缺失或與視窗按鈕重疊');
        await native('acceptance.capture',{tag:'update'});notice.click();if(!$('#services-dialog').open)throw new Error('標題提示無法開啟更新視窗');
        await native('acceptance.capture',{tag:'services'});$('#services-dialog').close();
        await native('acceptance.done',{success:true,titleUpdateNotice:true,clickOpensUpdates:true,footerHighlight:$('#services-entry').classList.contains('has-update'),previewOnly:true});
      }catch(error){await native('acceptance.done',{success:false,error:error.message});}
    };
    window.safeCodeRunServicesAcceptance=async()=>{
      try{
        const windowSizing=await native('acceptance.window-sizes');const prefs=await native('services.preferences');showPreferences(prefs);if(prefs.enabled||prefs.diagnosticsEnabled)throw new Error('首次統計或診斷預設值錯誤');
        await native('acceptance.capture',{tag:'notice'});if($('#exit').getBoundingClientRect().bottom>innerHeight||$('#usage-consent').getBoundingClientRect().bottom>$('#accept').getBoundingClientRect().top)throw new Error('聲明頁佈局溢出');
        await native('services.consent',{enabled:false,confirmed:false});const disabled=await native('services.preferences');if(disabled.enabled)throw new Error('關閉統計失敗');showPreferences(disabled);
        $('#services-dialog').showModal();if($('#feedback-send').getBoundingClientRect().bottom>$('#services-dialog').getBoundingClientRect().bottom)throw new Error('回饋按鈕超出視窗');await checkUpdate(true);await native('acceptance.capture',{tag:'services'});const feedbackMarker='SafeCode Windows integration test '+crypto.randomUUID();await native('feedback.send',{message:feedbackMarker});$('#services-dialog').close();
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
          if($('#workspace-screen').hidden||!$('#developer-preview'))throw new Error('開發預覽沒有正常進入');
          switchMode('maintenance');if($('#maintenance-panel').hidden)throw new Error('維護模式不可見');
          await native('acceptance.capture',{tag:'maintenance'});
          await native('acceptance.done',{success:true,developerPreview:true,noServer:true});
        }catch(error){await native('acceptance.done',{success:false,error:error.message});}return;
      }

      const waitFor=async predicate=>{const end=Date.now()+15000;while(!predicate()){if(Date.now()>end)throw new Error('等待介面超時');await new Promise(r=>setTimeout(r,30));}};
      const centered=dialog=>{const b=dialog.getBoundingClientRect();if(Math.abs(b.left+b.width/2-innerWidth/2)>2||Math.abs(b.top+b.height/2-innerHeight/2)>2||b.right>innerWidth||b.bottom>innerHeight)throw new Error('彈出視窗沒有居中或超出視窗');};
      try{
        await startupReady;
        if(config.phase==='restart'){
          if($('#login-screen').hidden||!$('#notice-screen').hidden)throw new Error('再次啟動仍顯示聲明');
          await native('acceptance.capture',{tag:'login'});await native('acceptance.done',{success:true,noticeSkippedAfterRestart:true});return;
        }
        $('#usage-consent').checked=false;await $('#accept').onclick();if(!(await native('notice.state')).accepted)throw new Error('聲明確認未儲存');
        advanced.click();centered($('#advanced-dialog'));await native('acceptance.capture',{tag:'advanced'});$('#advanced-dialog .close').click();if($('#advanced-dialog').open)throw new Error('進階連線無法關閉');
        advanced.click();$('#ssh-port').value='2222';$('#advanced-form').requestSubmit();if(sshPort!==2222||$('#advanced-dialog').open)throw new Error('SSH 連接埠未儲存');
        const settings={host:'127.0.0.1',username:'fixture',password:'fixture-only',port:config.silentPort};
        const pendingConnect=connect(settings);if($('#workspace-screen').hidden||!$('#deployment-panel').inert)throw new Error('連線時未立即顯示主介面或操作未鎖定');
        await native('acceptance.capture',{tag:'loading'});await $('#cancel-connect').onclick();await pendingConnect;if(!$('#connection-progress').classList.contains('failed'))throw new Error('取消未生效');
        const failure=connect({...settings,port:1});await waitFor(()=>$('#app-message-dialog').open);centered($('#app-message-dialog'));
        if(!$('#app-message-body').textContent.includes('伺服器連線不上')||$('#app-message-body').textContent.includes('timed out'))throw new Error('連線失敗未轉換為中文提示');
        await native('acceptance.capture',{tag:'error'});$('#app-message-dialog [data-dialog-dismiss]').click();await failure;if($('#app-message-dialog').open)throw new Error('報錯彈出視窗無法關閉');
        const change={key:{host:'203.0.113.10',port:22,previousFingerprint:'SHA256:fixture-old',fingerprint:'SHA256:fixture-new'}};
        const no=confirmServerIdentity(change);await waitFor(()=>$('#app-message-dialog').open);centered($('#app-message-dialog'));await native('acceptance.capture',{tag:'trust'});$('#app-message-cancel').click();if(await no)throw new Error('否被當成同意');
        const yes=confirmServerIdentity(change);await waitFor(()=>$('#app-message-dialog').open);$('#app-message-confirm').click();if(!await yes)throw new Error('是未返回確認');
        await native('acceptance.done',{success:true,immediateWorkspace:true,connectingOperationsLocked:true,cancelResponsive:true,centeredErrors:true,friendlyNetworkError:true,advancedPortSaved:true,dialogsClosable:true,reinstallYesNo:true,noticeSaved:true});
      }catch(e){await native('acceptance.done',{success:false,error:e.message});}
    };
    window.safeCodeRunMaintenanceAcceptance=async()=>{
      try{
        await startupReady;const result=await native('acceptance.connect-current');
        applyDeploymentSettings(await native('deployment.settings.get'));
        if(!currentLocale())throw new Error('工作帳戶更新驗收需要先為該連線手動儲存本土化設定。');
        $('#username').value=result.username;nativeServer=result.server;connectionReady=true;busy=false;showScreen('workspace');lockWorkspace(false);applyServer(result.server);
        const before=await native('acceptance.hostname');await openAccountSettings();$('#custom-hostname').value='safe-draft-verification';$('#custom-username').value=result.server.username;$('#custom-password').value='fixture-password-1234';
        const saving=$('#account-settings-form').onsubmit({preventDefault(){}});const warningDeadline=Date.now()+5000;while(!$('#app-message-dialog').open&&Date.now()<warningDeadline)await new Promise(resolve=>setTimeout(resolve,20));if(!$('#app-message-dialog').open)throw new Error('再次更改帳戶未彈出確認');$('#app-message-cancel').click();await saving;
        if(await native('acceptance.hostname')!==before)throw new Error('取消更改仍修改了伺服器');
        // 後續只驗證草稿存儲；真實應用流程由隔離 UI 夾具覆蓋，不將範例密碼應用到驗收伺服器。
        await native('account.draft.save',{username:result.server.username,hostname:'safe-draft-verification',password:'fixture-password-1234'});accountOptions={username:result.server.username,hostname:'safe-draft-verification',password:'fixture-password-1234'};accountDraftPending=true;accountSettingsSaved=true;$('#custom-password').value='';$('#account-settings').close();renderTasks();
        if(!accountDraftPending||states[0]!=='done'||$('#steps .step:first-child > .deploy-button:not(.locale-settings-button)').textContent!=='更新'||$('#steps .step:first-child > .deploy-button:not(.locale-settings-button)').disabled)throw new Error('已完成步驟未提供更新');
        if(await native('acceptance.hostname')!==before)throw new Error('儲存參數修改了伺服器');
        await openAccountSettings();if($('#custom-hostname').value!=='safe-draft-verification'||$('#account-settings-form button[type=submit]').textContent!=='儲存')throw new Error('參數儲存或按鈕文案錯誤');await native('acceptance.capture',{tag:'draft'});$('#account-settings').close();await native('account.draft.save',{username:result.server.username,hostname:before,password:''});accountOptions={username:result.server.username,hostname:before,password:''};accountDraftPending=true;await deployOne(0);if(accountDraftPending||states[0]!=='done'||await native('acceptance.hostname')!==before)throw new Error('帳戶更新應用未通過或更改了測試伺服器名稱');
        switchMode('maintenance');await native('acceptance.capture',{tag:'maintenance'});$('#desktop-resolution').click();await new Promise(r=>setTimeout(r,300));$('#resolution-select').value='1920x1080';await $('#resolution-form').onsubmit({preventDefault(){}});const setting=await native('desktop.resolution.get');if(setting.width!==1920||setting.height!==1080)throw new Error('解像度未儲存');await $('#desktop-resolution').onclick();await native('acceptance.capture',{tag:'resolution'});$('#resolution-dialog').close();await native('desktop.resolution.save',{width:1280,height:800});
        await installAdvanced('mount');if(!nativeServer.mountReady)throw new Error('目錄掛載組件未安裝成功');if(!advancedOperation.lines.length)throw new Error('掛載安裝無日誌');await native('acceptance.capture',{tag:'install'});$('#operation-log-dialog').close();
        let cloud=null;if(!nativeServer.cloudReady){cloud=installAdvanced('cloud');await new Promise(r=>setTimeout(r,500));$('#operation-log-dialog').close();}
        await $('#mount-settings').onclick();$('#remote-folder-dialog').showModal();const started=Date.now();await listRemote();if($('#folder-confirm').disabled)throw new Error('目錄讀取失敗：'+$('#folder-error').textContent);const folderDuration=Date.now()-started;if(folderDuration>12000)throw new Error('目錄讀取未受超時約束');await native('acceptance.capture',{tag:'folder'});$('#remote-folder-dialog').close();$('#mount-dialog').close();
        if(cloud)await cloud;if(!nativeServer.cloudReady)throw new Error('CloudCLI 安裝未完成');const page=await native('acceptance.cloud-http');if(page.status!==200)throw new Error('CloudCLI 網頁無法開啟');$('#operation-log-dialog').close();
        await native('acceptance.done',{success:true,draftSaveDidNotApply:true,completedAccountUpdateEnabled:true,idempotentAccountUpdateApplied:true,resolutionPersisted:true,mountInstalled:nativeServer.mountReady,cloudInstalled:nativeServer.cloudReady,cloudHttp:page.status,folderReadMilliseconds:folderDuration,folderReadSucceeded:true,hostnameBefore:before});
      }catch(e){await native('acceptance.done',{success:false,error:e.message});}
    };
    if(isMac){
      for(const element of document.querySelectorAll('.host-permission-note p'))element.textContent=element.textContent.replaceAll('Windows','macOS');
      document.querySelector('.host-risk small').textContent='需先在系統設定中開啟遠端登入，僅允許當前帳戶。使用專用密鑰，不會修改系統 SSH 設定。';
      $('#host-alias').placeholder='例如 mac 或 local';
      renderClaudeBridge();
      $('#bridge-manage').title='查看第三方功能警告';
      renderDesktopAccess();
    }
    window.safeCodeNativeReady=true;
  })();
}
