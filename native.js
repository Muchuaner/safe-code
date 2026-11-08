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
    const ids = ['account','localization','desktop','chrome','input','claude','ssh'];
    const native = (op,args={}) => new Promise((resolve,reject) => {
      const id=crypto.randomUUID();pending.set(id,{resolve,reject});window.chrome.webview.postMessage({id,op,args});
    });
    window.safeCodeNativeResponse = ({id,result,error}) => {
      const item=pending.get(id);if(!item)return;pending.delete(id);error?item.reject(new Error(error)):item.resolve(result);
    };
    window.safeCodeNativeLog = (step,line) => {
      if(currentTask>=0){taskLogs[currentTask]=line;renderTasks();}
      else if($('#login-screen').hidden===false){$('#connection-message').textContent=line;}
      else if($('#install-cloud').textContent==='安装中…'){$('#cloud-state').textContent=line.slice(-60);}
    };
    const nativeStyle=document.createElement('style');nativeStyle.textContent=`body{padding:0;display:block;background:white;min-height:100vh}.window,.window.wide,.window.maximized{width:100%;height:100vh;max-width:none;border:0;border-radius:0;box-shadow:none;transition:none}.header-controls{width:112px}.maintenance-panel .advanced-actions>span{overflow:hidden;white-space:nowrap;text-overflow:ellipsis;max-width:100%}.connection-message{overflow:hidden;white-space:nowrap;text-overflow:ellipsis}#login-screen{padding-top:23px}.login-heading{margin-top:0;margin-bottom:14px}#login-form input:not([type=checkbox]){height:36px;margin-bottom:12px}#login-form .password-wrap input{margin-bottom:0}#login-form .connection-message{height:27px;padding-top:8px}#login-form .primary{height:36px}#login-screen>.back{margin-top:10px}.native-log-note{white-space:normal;line-height:1.6;color:#99a599;font-size:9px}`;document.head.append(nativeStyle);
    $('#minimize').onclick=()=>native('window.minimize');
    $('#maximize').onclick=()=>native('window.maximize');
    $('#close').onclick=()=>native('window.close');$('#exit').onclick=()=>native('window.close');
    $('.titlebar').addEventListener('mousedown',e=>{if(e.button===0&&!e.target.closest('button'))native('window.drag');});
    $('.titlebar').addEventListener('dblclick',e=>{if(!e.target.closest('button'))native('window.maximize');});
    const originalShowScreen=showScreen;showScreen=function(name){originalShowScreen(name);native('window.resize',{wide:name==='workspace'});};
    const advanced=document.createElement('button');advanced.type='button';advanced.className='plain';advanced.textContent='高级连接';advanced.style.marginTop='8px';
    advanced.onclick=()=>{const value=prompt('SSH 端口',String(sshPort));if(value!==null){const port=Number(value);if(!Number.isInteger(port)||port<1||port>65535){alert('端口须为 1–65535。');return;}sshPort=port;}};
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
      $('#rating-state').textContent='查询中';$('#ip-location').textContent='正在查询…';$('#ip-score').textContent='—';$('#score-unit').textContent='';
      try{
        const info=await native('ip.info',{ip});
        $('#ip-location').textContent=info.location||'暂无归属地';$('#ip-score').textContent=info.score===null?'—':String(info.score);$('#score-unit').textContent=info.score===null?'':' / 100';$('#rating-state').textContent=info.geoSource;
        $('#score-source').href='https://ip.net.coffee/ip/'+encodeURIComponent(ip);$('#score-source').title='定位：'+info.geoSource+(info.hasGeoConflict?'（部分来源存在分歧）':'')+'；评分为第三方'+(info.isSubnetScore?'网段':'IP')+'评分。';
        const regionMap={jp:'jp',sg:'sg',gb:'gb',de:'de',kr:'kr'};detectedRegion=info.regionCode==='us'?({'New York':'us_ny','Los Angeles':'us_la'}[info.city]||null):(regionMap[info.regionCode]||null);
      }catch{detectedRegion=null;$('#rating-state').textContent='暂不可用';$('#ip-location').textContent='暂无归属地';$('#ip-score').textContent='—';}
      if(nativeServer)applyServer(nativeServer);
    };
    async function connect(settings){
      $('#login-form').querySelectorAll('input,button').forEach(el=>el.disabled=true);$('#back').disabled=true;
      $('#connect').textContent='正在连接…';setMetrics(null);
      try{
        const result=await native('connect',{host:settings.host,username:settings.username,password:settings.password,port:settings.port||sshPort});
        $('#password').value='';serverConnection={ingressIp:result.ipPolicy?.ingressIp||settings.host,egressIp:result.ipPolicy?.egressIp||result.observedEgressIp||''};$('#server-ingress').textContent=serverConnection.ingressIp;$('#server-address').textContent=serverConnection.egressIp||'待确认';
        taskLogs=Array(tasks.length).fill('');selecting=false;selectedTasks.clear();busy=false;detectedRegion=null;applyServer(result);switchMode('deployment');showScreen('workspace');
        clearInterval(metricsTimer);const measure=async()=>{if(metricsBusy)return;metricsBusy=true;try{setMetrics(await native('metrics'));}catch{setMetrics({latencyMs:null,lossPercent:100});}finally{metricsBusy=false;}};metricsTask=measure();metricsTimer=setInterval(measure,5000);
        await queryIpInfo(serverConnection.egressIp);applyServer(result);if(result.activeJobs?.length)resumeJob(result.activeJobs[0]);return result;
      }finally{resetLogin();}
    }
    $('#login-form').onsubmit=async e=>{e.preventDefault();try{await connect({host:$('#ip').value.trim(),username:$('#username').value.trim(),password:$('#password').value,port:sshPort});}catch(error){$('#connection-message').className='connection-message error';$('#connection-message').textContent=error.message;alert(error.message);}};
    async function resumeJob(job){busy=true;currentTask=ids.indexOf(job.operation);if(currentTask>=0){states[currentTask]='running';taskLogs[currentTask]='恢复远端部署监控…';}renderTasks();try{applyServer(await native('job.resume',job));}catch(e){alert(e.message);}finally{currentTask=-1;busy=false;renderTasks();}}
    async function runTask(index){
      currentTask=index;states[index]='running';taskLogs[index]='正在连接 Ubuntu 部署模块…';renderTasks();
      try{
        const preset=currentLocale();
        const locale=preset?{region:preset.name,timezone:preset.timezone,locale:preset.locale,language:preset.language}:null;
        const result=await native('deploy',{step:ids[index],locale,username:accountOptions.username||nativeServer?.username||'workspace',hostname:accountOptions.hostname||'',password:accountOptions.password||''});applyServer(result);
        if(!result.statuses?.[ids[index]])throw new Error(tasks[index][0]+'验证未通过，请查看日志。');
        taskLogs[index]='部署完成，已通过实际状态检查。';if(index===0)accountOptions={};
      }catch(error){states[index]='pending';taskLogs[index]='失败：'+error.message;throw error;}
      finally{currentTask=-1;renderTasks();}
    }
    deployOne=async function(index){if(busy||states[index]==='done')return;selecting=false;selectedTasks.clear();busy=true;try{await runTask(index);}catch(error){alert(error.message);}finally{busy=false;renderTasks();}};
    $('#deploy-all').onclick=async()=>{
      if(busy)return;if(!selecting){selecting=true;selectedTasks.clear();renderTasks();return;}
      const queue=[...selectedTasks].filter(i=>states[i]!=='done').sort((a,b)=>a-b);if(!queue.length)return;
      selecting=false;selectedTasks.clear();busy=true;
      try{for(const i of queue)await runTask(i);}catch(error){alert(error.message);}finally{busy=false;renderTasks();}
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
      }catch(error){alert(error.message);}
    };
    async function runMaintenanceCard(selector,action){const card=$(selector);const caption=card.querySelector('span');const text=caption.textContent;card.disabled=true;caption.textContent='正在连接…';try{await action();}catch(e){alert(e.message);}finally{card.disabled=false;caption.textContent=text;}}
    $('#desktop-card').onclick=()=>runMaintenanceCard('#desktop-card',()=>native('desktop'));
    $('#terminal-card').onclick=()=>runMaintenanceCard('#terminal-card',async()=>{const result=await native('terminal');if(result)applyServer(result);});
    $('#install-cloud').onclick=async()=>{
      if(busy)return;busy=true;$('#install-cloud').disabled=true;$('#install-cloud').textContent='安装中…';
      try{applyServer(await native('cloud.install'));}catch(error){$('#cloud-state').textContent='安装失败';$('#install-cloud').disabled=false;$('#install-cloud').textContent='重试';alert(error.message);}finally{busy=false;renderTasks();}
    };
    $('#open-cloud').onclick=()=>native('cloud.open').catch(e=>alert(e.message));
    $('#install-mount').onclick=async()=>{
      if(busy)return;busy=true;$('#install-mount').disabled=true;$('#install-mount').textContent='安装中…';
      try{applyServer(await native('mount.install'));}catch(error){$('#mount-state').textContent='安装失败';$('#install-mount').disabled=false;$('#install-mount').textContent='重试';alert(error.message);}finally{busy=false;renderTasks();}
    };
    let selectedRemoteField=null;
    async function listRemote(path=''){
      $('#folder-error').textContent='';$('#folder-list').textContent='正在读取…';
      try{const result=await native('folder.list',{path});$('#folder-path').textContent=result.path;$('#folder-up').disabled=result.path===result.parent;$('#folder-up').onclick=()=>listRemote(result.parent);$('#folder-list').replaceChildren();for(const dir of result.directories){const button=document.createElement('button');button.textContent=dir.name;button.onclick=()=>listRemote(dir.path);$('#folder-list').append(button);}if(!result.directories.length)$('#folder-list').textContent='此目录没有下级文件夹。';$('#folder-confirm').disabled=false;}
      catch(e){$('#folder-list').textContent='';$('#folder-error').textContent=e.message;$('#folder-confirm').disabled=true;}
    }
    $('#folder-confirm').onclick=()=>{if(selectedRemoteField)selectedRemoteField.value=$('#folder-path').textContent;$('#remote-folder-dialog').close();};
    const originalAppendMountPair=appendMountPair;appendMountPair=function(pair){originalAppendMountPair(pair);const row=$('#mount-list').lastElementChild;for(const key of ['local','remote']){const field=row.querySelector('[data-path='+key+']');const wrap=document.createElement('span');wrap.className='folder-input';field.replaceWith(wrap);wrap.append(field);const picker=document.createElement('button');picker.type='button';picker.textContent='选择目录';picker.onclick=async()=>{if(key==='local'){try{const selected=await native('pickFolder');if(selected)field.value=selected;}catch(e){alert(e.message);}}else{selectedRemoteField=field;$('#remote-folder-dialog').showModal();await listRemote();}};wrap.append(picker);}};
    openAccountSettings=async function(){try{$('#custom-hostname').value=accountOptions.hostname||nativeServer?.hostname||'safe-workstation';$('#custom-username').value=accountOptions.username||nativeServer?.username||'workspace';$('#custom-password').value='';$('#account-custom-hint').textContent='更换用户名会新建环境，保留旧账户与项目。请先注销旧桌面并卸载目录；旧 CloudCLI 将停止。';$('#account-settings').showModal();}catch(e){alert(e.message);}};
    $('#account-settings-form').onsubmit=async e=>{e.preventDefault();if(busy)return;accountOptions={username:$('#custom-username').value.trim(),hostname:$('#custom-hostname').value.trim(),password:$('#custom-password').value};busy=true;try{const server=await native('deploy',{step:'account',...accountOptions});applyServer(server);accountOptions={};$('#custom-password').value='';$('#account-settings').close();}catch(e){alert(e.message);}finally{busy=false;renderTasks();}};
    native('login.load').then(saved=>{if(saved&&$('#workspace-screen').hidden){$('#ip').value=saved.host;$('#username').value=saved.username;$('#password').value=saved.password;sshPort=saved.port||22;}}).catch(()=>{});
    $('#mount-settings').onclick=async()=>{try{mountPairs=await native('mount.settings');$('#mount-list').replaceChildren();(mountPairs.length?mountPairs:[{local:'',remote:''}]).forEach(appendMountPair);$('#mount-dialog').showModal();}catch(e){alert(e.message);}};
    $('#mount-form').onsubmit=async e=>{
      e.preventDefault();const mappings=$$('#mount-list .mount-pair').map(row=>({local:row.querySelector('[data-path="local"]').value.trim(),remote:row.querySelector('[data-path="remote"]').value.trim()}));
      try{await native('mount.apply',{mappings});mountPairs=mappings;$('#mount-summary').textContent='已挂载 '+mappings.length+' 组目录。';$('#mount-dialog').close();}catch(error){alert(error.message);}
    };
    $('#ip-policy-form').onsubmit=async e=>{
      e.preventDefault();const args={ingressIp:$('#ingress-ip-input').value.trim(),egressIp:$('#egress-ip-input').value.trim(),lockIngress:$('#lock-ingress').checked,lockEgress:$('#lock-egress').checked};
      try{const result=await native('ip.apply',args);serverConnection={ingressIp:args.ingressIp,egressIp:args.egressIp};$('#server-ingress').textContent=args.ingressIp;$('#server-address').textContent=args.egressIp;ipPolicy=args;$('#ip-policy-state').textContent='已应用';$('#ip-policy-dialog').close();if(result)applyServer(result);await queryIpInfo(args.egressIp);}catch(error){alert(error.message);}
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
    native('services.preferences').then(showPreferences).catch(()=>{$('#usage-consent').checked=false;$('#settings-usage-consent').checked=false;}).finally(()=>{$('#accept').disabled=false;for(const id of ['usage-consent','settings-usage-consent'])document.getElementById(id).disabled=false;});
    for(const id of ['usage-consent','settings-usage-consent'])document.getElementById(id).onchange=e=>{const enabled=e.target.checked;$('#usage-consent').checked=enabled;$('#settings-usage-consent').checked=enabled;native('services.consent',{enabled,confirmed:false}).then(showPreferences).catch(e=>alert(e.message));};
    const previousAccept=$('#accept').onclick;$('#accept').onclick=()=>{previousAccept();native('services.consent',{enabled:$('#usage-consent').checked,confirmed:true}).then(showPreferences).catch(()=>{});};
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
        const prefs=await native('services.preferences');showPreferences(prefs);if(!prefs.enabled)throw new Error('首次统计默认值错误');
        await native('acceptance.capture',{tag:'notice'});if($('#exit').getBoundingClientRect().bottom>innerHeight||$('#usage-consent').getBoundingClientRect().bottom>$('#accept').getBoundingClientRect().top)throw new Error('声明页布局溢出');
        await native('services.consent',{enabled:false,confirmed:false});const disabled=await native('services.preferences');if(disabled.enabled)throw new Error('关闭统计失败');showPreferences(disabled);
        $('#services-dialog').showModal();if($('#feedback-send').getBoundingClientRect().bottom>$('#services-dialog').getBoundingClientRect().bottom)throw new Error('反馈按钮超出窗口');await checkUpdate(true);await native('acceptance.capture',{tag:'services'});const feedbackMarker='SafeCode Windows integration test '+crypto.randomUUID();await native('feedback.send',{message:feedbackMarker});$('#services-dialog').close();
        await native('services.consent',{enabled:true,confirmed:false});
        await native('acceptance.done',{success:true,defaultEnabled:prefs.enabled,optOutSaved:!disabled.enabled,serviceAvailable:prefs.serviceAvailable,recipient:prefs.recipient,feedbackMarker,feedbackSentWithStatsDisabled:true,updateStatus:$('#update-status').textContent,viewportHeight:innerHeight});
      }catch(error){await native('acceptance.done',{success:false,error:error.message});}
    };
    window.safeCodeNativeReady=true;
  })();
}
