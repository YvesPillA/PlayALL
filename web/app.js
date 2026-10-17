import {renderMap,zoomMap} from "./map.js";

const $ = selector => document.querySelector(selector);
let settings, state, token, cursor=0, pending=false, timer, editId="deepseek", toastTimer;
const statusNames={idle:"准备就绪",preparing:"准备游戏",running:"正在运行",paused:"已暂停",stopping:"正在保存",stopped:"已停止",completed:"本局完成",limited:"到达上限",error:"需要处理"};
const itemNames={"iron-plate":"铁板","copper-plate":"铜板","iron-ore":"铁矿石","copper-ore":"铜矿石",coal:"煤",stone:"石头",wood:"木材","stone-furnace":"石炉","burner-mining-drill":"热能采矿机","iron-gear-wheel":"铁齿轮","iron-stick":"铁棒","transport-belt":"传送带",inserter:"机械臂","burner-inserter":"热能机械臂","small-electric-pole":"小型电线杆","electronic-circuit":"电子电路","automation-science-pack":"红色科技包","steam-engine":"蒸汽机",boiler:"锅炉","offshore-pump":"供水泵",lab:"实验室","assembling-machine-1":"组装机 1","wooden-chest":"木箱","copper-cable":"铜线"};
const actionNames={observe:"观察现场",move:"移动",mine:"开采",craft:"手工制作",build:"建造",insert:"放入物品",extract:"取回物品",set_recipe:"设置配方",research:"开始科研",wait:"等待生产",inspect:"检查详情",recipes:"查看配方",rotate:"旋转建筑",deconstruct:"回收建筑"};
const colors={"iron-plate":"#91a2b7","iron-ore":"#91a2b7","copper-plate":"#c58b55","copper-ore":"#c58b55",coal:"#435a66",stone:"#b6ad89",wood:"#a78862","stone-furnace":"#ae9471","burner-mining-drill":"#ca9d4e","iron-gear-wheel":"#7892a7"};

function element(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
function toast(message,error=false){clearTimeout(toastTimer);const box=$("#toast");box.textContent=String(message).slice(0,900);box.classList.toggle("error",error);box.hidden=false;toastTimer=setTimeout(()=>box.hidden=true,error?9000:4500);}
async function api(path,data){const response=await fetch("/api/"+path,{method:data===undefined?"GET":"POST",headers:data===undefined?{}:{"Content-Type":"application/json","X-PlayALL-Token":token},body:data===undefined?undefined:JSON.stringify(data)});const result=await response.json();if(!response.ok)throw Error(result.error||"操作没有成功，请重试。");return result;}
function selectedProfile(){return settings.profiles.find(profile=>profile.id===$("#provider").value);}
function needsKey(){const profile=selectedProfile();return profile.id!=="rehearsal"&&!profile.has_key&&!/^http:\/\/(localhost|127\.0\.0\.1)(:|\/|$)/.test(profile.base_url);}

function renderConfiguration(){
  for(const option of $("#provider").options){const p=settings.profiles.find(item=>item.id===option.value);if(p)option.textContent=p.label;}
  const p=selectedProfile(), note=$("#provider-note");
  note.textContent=p.id==="rehearsal"?"固定脚本演练，零 API 费用。":needsKey()?"先在设置里填写 API Key，即可开始。":p.model+" · 已配置";
  note.className="field-note "+(needsKey()?"attention":"ready");
  const game=$("#game").value, gn=$("#game-note");
  gn.textContent=game==="sandbox"?"独立内置小游戏，不是 Factorio。":settings.factorio_detected?"已找到安装 · AI 使用专用和平地图。":"未找到安装，请在设置里选择游戏文件。";
  gn.className="field-note "+(game==="factorio"&&!settings.factorio_detected?"attention":"ready");
  $("#budget-caption").textContent=`每局最多 ${settings.max_turns} 轮，费用估算上限 $${Number(settings.max_cost).toFixed(2)}`;
  $("#goal").disabled=p.id==="rehearsal"||Boolean(state?.active);
  $("#goal").title=p.id==="rehearsal"?"免费脚本使用固定目标：实际冶炼并取回 10 块铁板。":"";
  if(!state?.active)$("#start-label").textContent=needsKey()?"先设置 API Key":p.id==="rehearsal"?"开始免费演练":"开始，让 AI 来玩";
}

function renderInventory(observation){
  const items=Object.entries(observation?.inventory||{}).filter(([,count])=>count>0).sort((a,b)=>b[1]-a[1]);
  const holder=$("#inventory");holder.replaceChildren();
  if(!items.length){holder.append(element("div","inventory-empty",observation?"背包暂时是空的。":"进入游戏后，物品会出现在这里。"));}
  for(const [name,count]of items){const item=element("div","item");item.title=name;item.style.setProperty("--item-color",colors[name]||"#96a8b7");item.append(element("i","item-symbol"),element("span","",itemNames[name]||name),element("b","",String(count)));holder.append(item);}
  $("#inventory-count").textContent=observation?`${items.length} 种物品`:"未连接";
}

function appendEvents(events){
  const feed=$("#activity-feed"), nearBottom=feed.scrollHeight-feed.scrollTop-feed.clientHeight<80;
  if(events.length&&feed.querySelector(".activity-empty"))feed.replaceChildren();
  const marks={thought:"✦",action:"›",result:"✓",error:"!",success:"✓",notice:"·",system:"·"};
  for(const event of events){
    const row=element("div","event "+event.kind),time=element("time","",new Date(event.time*1000).toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit",hour12:false}));
    const content=element("div","event-content",event.kind==="action"?(actionNames[event.action?.action]||event.message):event.message);
    if(event.action){const a=event.action;const bits=[];if(a.name)bits.push(itemNames[a.name]||a.name);if(a.count)bits.push(`× ${a.count}`);if(a.x!==undefined)bits.push(`位置 ${Number(a.x).toFixed(1)}, ${Number(a.y).toFixed(1)}`);if(a.seconds)bits.push(`${a.seconds} 游戏秒`);content.append(element("span","action-detail",bits.join("，")));}
    row.append(time,element("span","event-mark",marks[event.kind]||"·"),content);feed.append(row);
  }
  while(feed.childElementCount>150)feed.firstElementChild.remove();
  if(nearBottom)feed.scrollTop=feed.scrollHeight;
}

function renderState(next){
  state=next;const active=Boolean(state.active),observation=state.observation;
  $("#status-badge").className="session-badge "+state.status;$("#status-badge b").textContent=statusNames[state.status]||state.status;
  $("#status-message").textContent=state.message;
  $("#start-button").hidden=active;$("#running-controls").hidden=!active;
  $("#pause-button").textContent=state.status==="paused"?"继续":"暂停";
  $("#pause-button").disabled=pending||state.status==="stopping";
  $("#stop-button").disabled=pending||state.status==="stopping";
  $("#rehearsal-button").hidden=active;$("#start-button").disabled=pending;
  for(const selector of["#provider","#game","#new-world"]){$(selector).disabled=active||pending;}
  document.querySelectorAll("[data-goal]").forEach(button=>button.disabled=active);
  $("#watch-button").disabled=!active||state.game!=="factorio"||!observation||state.status==="stopping";
  $("#stage-title").textContent=observation?(state.game==="factorio"?"Factorio":"采矿练习场"):"游戏现场";
  $("#stage-source").textContent=observation?observation.source:"等待连接";
  $("#stage-dot").classList.toggle("live",Boolean(observation)&&active);
  $("#empty-stage").hidden=Boolean(observation);
  $("#mission-label").textContent=state.provider==="rehearsal"?"免费脚本演练":active?"本局状态":"本局状态";
  $("#production").hidden=!observation;
  $("#plate-count").textContent=String(observation?.iron_plates_produced||0);
  $("#coordinates").textContent=observation?`位置 ${observation.position.x.toFixed(1)}, ${observation.position.y.toFixed(1)}`:"尚未进入游戏";
  $("#activity-model").textContent=state.model?(state.provider==="rehearsal"?"固定脚本 · 未调用 AI":state.model):"行动会实时出现在这里";
  $("#turn-count").textContent=String(state.turns||0);$("#token-count").textContent=Number(state.total_tokens||0).toLocaleString();$("#cost-count").textContent="$"+Number(state.cost||0).toFixed(3);
  renderInventory(observation);renderMap(observation);appendEvents(state.events||[]);cursor=state.event_cursor||cursor;renderConfiguration();
}

function loadProfile(id){
  editId=id;const p=settings.profiles.find(item=>item.id===id);$("#edit-provider").value=id;
  $("#api-key").value="";$("#api-key").placeholder=p.has_key?"已保存 · 留空保留原密钥":"粘贴 API Key";
  $("#key-note").textContent=p.has_key?"已有加密密钥；留空不会清除。":"密钥只在本机加密保存，不会显示在行动记录里。";
  $("#model").value=p.model;$("#base-url").value=p.base_url;$("#input-price").value=p.input_price;$("#output-price").value=p.output_price;$("#probe-result").hidden=true;
}
function openSettings(){
  loadProfile($("#provider").value==="custom"?"custom":"deepseek");
  for(const[key,id]of[["factorio_path","factorio-path"],["max_turns","max-turns"],["max_cost","max-cost"],["max_tokens","max-tokens"],["speed","speed"]])$("#"+id).value=settings[key];
  $("#settings-message").textContent="修改后的配置在下一局生效。";$("#settings-dialog").showModal();
}
async function saveSettings(close=true){
  if(!$("#settings-form").reportValidity())throw Error("请检查设置中的数值。");
  const patch={id:editId,api_key:$("#api-key").value,model:$("#model").value,base_url:$("#base-url").value,input_price:Number($("#input-price").value),output_price:Number($("#output-price").value)};
  const result=await api("settings",{profiles:[patch],factorio_path:$("#factorio-path").value,max_turns:Number($("#max-turns").value),max_cost:Number($("#max-cost").value),max_tokens:Number($("#max-tokens").value),speed:Number($("#speed").value)});
  settings=result.settings;$("#api-key").value="";renderConfiguration();
  if(close){$("#settings-dialog").close();toast("设置已保存。");}else{$("#settings-message").textContent="配置已保存。";}
}
async function withPending(action){if(pending)return;pending=true;if(state)renderState({...state,events:[]});try{await action();}catch(error){toast(error.message,true);}finally{pending=false;await poll();}}
async function start(rehearsal=false){
  if(!rehearsal&&needsKey()){openSettings();return;}
  if($("#game").value==="factorio"&&!settings.factorio_detected){openSettings();toast("先在设置里选择 Factorio 的 factorio.exe。",true);return;}
  await withPending(async()=>{
    const provider=rehearsal?"rehearsal":$("#provider").value,game=$("#game").value,goal=$("#goal").value.trim();
    if(!goal)throw Error("告诉 AI 这局想做什么。");
    const saved=await api("settings",{provider:$("#provider").value,game,goal});settings=saved.settings;
    await api("start",{provider,game,goal,new_world:$("#new-world").checked});
    $("#activity-feed").replaceChildren();cursor=0;$("#new-world").checked=false;
  });
}
async function poll(){
  try{const result=await api("state?after="+cursor);renderState(result);$("#connection").textContent="本机运行";$(".connection-dot").style.background="var(--green)";}
  catch(error){$("#connection").textContent="连接已断开";$(".connection-dot").style.background="var(--red)";$("#status-message").textContent="本地终端连接已断开，请重新双击启动 PlayALL。";}
}

$("#settings-button").addEventListener("click",openSettings);
$("#edit-provider").addEventListener("change",event=>loadProfile(event.target.value));
$("#settings-form").addEventListener("submit",async event=>{event.preventDefault();const button=$("#save-settings");button.disabled=true;try{await saveSettings();}catch(error){$("#settings-message").textContent=error.message;toast(error.message,true);}finally{button.disabled=false;}});
$("#probe-button").addEventListener("click",async()=>{const button=$("#probe-button"),result=$("#probe-result");button.disabled=true;button.textContent="连接中…";try{await saveSettings(false);const data=await api("probe",{provider:editId});result.textContent=data.message;result.className="inline-result"+(data.ok?"":" error");const options=$("#model-options");options.replaceChildren(...data.models.map(id=>{const option=element("option");option.value=id;return option;}));}catch(error){result.textContent=error.message;result.className="inline-result error";}finally{result.hidden=false;button.disabled=false;button.textContent="测试连接";}});
$("#pick-factorio").addEventListener("click",async()=>{try{const result=await api("pick-factorio",{});if(result.path)$("#factorio-path").value=result.path;}catch(error){toast(error.message,true);}});
$("#start-button").addEventListener("click",()=>start());$("#rehearsal-button").addEventListener("click",()=>start(true));
$("#pause-button").addEventListener("click",()=>withPending(()=>api(state.status==="paused"?"resume":"pause",{})));
$("#stop-button").addEventListener("click",()=>withPending(()=>api("stop",{})));
$("#watch-button").addEventListener("click",async()=>{try{const data=await api("watch",{});toast(data.message);}catch(error){toast(error.message,true);}});
$("#provider").addEventListener("change",renderConfiguration);$("#game").addEventListener("change",renderConfiguration);
document.querySelectorAll("[data-goal]").forEach(button=>button.addEventListener("click",()=>{$("#goal").value=button.dataset.goal;}));
document.querySelectorAll("[data-close]").forEach(button=>button.addEventListener("click",()=>$("#"+button.dataset.close).close()));
document.querySelectorAll("dialog").forEach(dialog=>dialog.addEventListener("click",event=>{if(event.target===dialog){const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close();}}));
$("#zoom-out").addEventListener("click",()=>zoomMap(.8));$("#zoom-in").addEventListener("click",()=>zoomMap(1.25));$("#zoom-reset").addEventListener("click",()=>zoomMap(0));
$("#history-button").addEventListener("click",async()=>{try{const data=await api("history"),holder=$("#history-list");holder.replaceChildren();if(!data.runs.length)holder.append(element("p","field-note","还没有游戏记录。开始一局后，会自动保存在这里。"));for(const run of data.runs){const row=element("article","history-row");row.append(element("h3","",run.goal),element("p","",`${run.provider_label} / ${run.game}，${run.turns} 轮，$${Number(run.cost).toFixed(3)}，${statusNames[run.status]||run.status}`),element("small","",run.message));holder.append(row);}$("#history-dialog").showModal();}catch(error){toast(error.message,true);}});
$("#diagnostics-button").addEventListener("click",async()=>{try{const data=await api("diagnostics"),holder=$("#diagnostics-list");holder.replaceChildren();for(const[label,value]of[["本地终端",`正常 · v${data.version}`],["Factorio 安装",data.factorio_detected?"已找到":"需要设置路径"],["专用存档",data.save_exists?"已保存，可接着玩":"首次开始时创建"],["REA 辅助工具",data.rea_available?"已安装":"未安装（不影响游戏运行）"]]){const row=element("div","diagnostic-row");row.append(element("span","",label),element("b","",value));holder.append(row);}holder.append(element("p","diagnostic-path","本机数据目录："+data.data_directory));$("#diagnostics-dialog").showModal();}catch(error){toast(error.message,true);}});

async function boot(){try{const data=await api("bootstrap");token=data.token;settings=data.settings;$("#provider").value=settings.provider;$("#game").value=settings.game;$("#goal").value=settings.goal;renderState(data.state);timer=setInterval(poll,650);}catch(error){toast("终端初始化失败："+error.message,true);}}
boot();
