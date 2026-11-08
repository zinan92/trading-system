
(function(){
  const $ai=(id)=>document.getElementById(id);
  const messages=$ai("parkAiChatMessages"), input=$ai("parkAiChatInput"), send=$ai("parkAiSend"), confirm=$ai("parkAiConfirm"), reject=$ai("parkAiReject"), snapshotBox=$ai("parkAiSnapshot"), status=$ai("parkAiChatStatus");
  let pending=null;
  const text=(value)=>value===null||value===undefined||value===""?"—":String(value);
  const addMessage=(value,kind="system")=>{if(!value)return;const node=document.createElement("div");node.className=`park-ai-chat-message ${kind}`;node.textContent=String(value);messages.appendChild(node);messages.scrollTop=messages.scrollHeight};
  const setStatus=(value)=>{status.textContent=String(value||"")};
  // #1348: what this reply was based on — the model call, the market price it saw, the account facts.
  function sourcesLine(sources){
    if(!sources||typeof sources!=="object")return null;
    const parts=[],model=sources.model,market=sources.market,account=sources.account;
    if(model){const secs=Number.isFinite(Number(model.elapsed_ms))&&model.elapsed_ms!==null?` · ${(Number(model.elapsed_ms)/1000).toFixed(1)} 秒`:"";const ok=["returned","ok"].includes(String(model.status||""));parts.push({text:`模型 ${text(model.provider)}${model.model?" "+model.model:""}${model.fallback_from?`（${model.fallback_from} 不可用后改用）`:""}${secs}${ok?"":` · ${text(model.status)}`}`,bad:!ok})}
    else parts.push({text:"本地规则解析，未调用模型",bad:false});
    if(market){const when=market.observed_at?String(market.observed_at).replace("T"," ").slice(0,16)+" UTC":"时间未知";const good=market.trusted===true&&market.fresh===true;parts.push({text:`行情 ${text(market.provider)}${market.symbol?" "+market.symbol:""} ${market.price===null||market.price===undefined?"无价格":Number(market.price).toLocaleString("en-US",{maximumFractionDigits:2})} · ${when} · ${good?"可信":"不可信"}`,bad:!good})}
    else parts.push({text:"未读取行情",bad:false});
    if(account)parts.push({text:`账户 权益 ${account.equity===null||account.equity===undefined?"未知":Number(account.equity).toLocaleString("en-US",{maximumFractionDigits:2})} · 持仓 ${text(account.open_positions)} · 挂单 ${text(account.open_orders)}`,bad:account.equity===null||account.equity===undefined});
    else parts.push({text:"未读取账户",bad:false});
    const node=document.createElement("div");node.className="park-ai-sources";node.dataset.sources="1";
    const label=document.createElement("span");label.textContent="依据";node.appendChild(label);
    parts.forEach(part=>{const item=document.createElement("span");item.textContent=part.text;if(part.bad)item.className="bad";node.appendChild(item)});
    return node;
  }
  const addSources=(sources)=>{const node=sourcesLine(sources);if(!node)return;messages.appendChild(node);messages.scrollTop=messages.scrollHeight};
  const valueFor=(source,key)=>source&&Object.prototype.hasOwnProperty.call(source,key)?source[key]:null;
  function fillLegacyForm(draft){
    const source=(draft&&draft.plan&&draft.plan.normalized_input)||(draft&&draft.normalized)||{};
    const risk=(draft&&draft.plan&&draft.plan.risk)||{};
    const click=(selector,attr,value)=>{if(value===null||value===undefined||value==="")return;const button=document.querySelector(`${selector}[${attr}="${String(value).replace(/"/g,"\\\"")}"]`);if(button&&!button.disabled)button.click()};
    click("#strategyTypeChoices [data-strategy-type]","data-strategy-type",source.strategy_type);
    click("#directionChoices [data-direction]","data-direction",source.direction);
    const fields={rangeLow:source.lower_price_boundary,rangeHigh:source.upper_price_boundary,leverage:source.maximum_leverage,dcaStop:source.stop_price,dcaTarget:source.take_profit_price,gridCount:source.order_count,gridNotional:risk.per_order_notional};
    Object.entries(fields).forEach(([id,value])=>{if(value===null||value===undefined||value==="")return;const field=$ai(id);if(!field)return;field.value=String(value);field.dispatchEvent(new Event("input",{bubbles:true}))});
    setStatus("Draft 已解析并填入左侧 Paper 表单；尚未下单。请检查 snapshot 后确认。");
  }
  function renderSnapshot(card,draft){
    const plan=(card&&card.plan)||((draft&&draft.plan)||{});
    const normalized=(card&&card.normalized_input)||plan.normalized_input||(draft&&draft.normalized)||{};
    const market=(card&&card.market)||plan.market||{};
    const risk=(card&&card.risk)||plan.risk||{};
    const portfolio=(card&&card.portfolio)||plan.portfolio||{};
    const rows=[
      ["方向 / 类型",`${text(normalized.direction)} / ${text(normalized.strategy_type)}`],
      ["价格区间",`${text(normalized.lower_price_boundary)} ~ ${text(normalized.upper_price_boundary)}`],
      ["当前价",text(market.price)],
      ["最大名义",text(risk.maximum_notional)],
      ["理论最大亏损",text(risk.theoretical_max_loss)],
      ["有效杠杆",`${text(risk.effective_leverage)}x`],
      ["旧仓处理",portfolio.disposition?JSON.stringify(portfolio.disposition):"无"],
      ["Risk Digest",text(card&&card.risk_digest||draft&&draft.risk_digest)],
      ["Digest",text(card&&card.plan_digest||draft&&draft.plan_digest)]
    ];
    if(String(normalized.strategy_type||"").toLowerCase()==="grid"){
      const entry=risk.grid_entry_range||{},hardStop=risk.hard_stop,prices=Array.isArray(risk.grid_rung_prices)?risk.grid_rung_prices:[];
      const rungs=Array.isArray(risk.grid_rungs)?risk.grid_rungs:[];
      rows.splice(2,0,["Grid Entry Range",`${text(entry.lower)} ~ ${text(entry.upper)}`],["Grid Spacing / Rungs",`${text(risk.grid_spacing)} / ${text(risk.order_count)}`],["Grid Hard Stop",typeof hardStop==="object"?JSON.stringify(hardStop):text(hardStop)],["Local stop",risk.local_stop_authorized===true?"Park explicitly authorized":"absent by default"],["TP geometry",rungs.length?rungs.map(row=>`${text(row.price)}→${text(row.take_profit)} (${text(row.side)})`).join(" · "):"证据不足"]);
    }
    snapshotBox.replaceChildren();
    const sealed=Boolean(card&&!draft);const holder=sealed?document.createElement("details"):snapshotBox;if(sealed){const summary=document.createElement("summary");summary.textContent=`上一张策略 Snapshot · ${text(normalized.direction)} ${text(normalized.strategy_type)} ${text(normalized.lower_price_boundary)}~${text(normalized.upper_price_boundary)}`;holder.appendChild(summary);snapshotBox.appendChild(holder)}
    const title=document.createElement("h4");title.textContent=card&&(card.sealed===true||card.status==="expired")?"已封存的不可变策略 Snapshot":card&&card.status==="confirmed"?"已确认的不可变策略 Snapshot":"Draft · 等待 Park 确认";holder.appendChild(title);
    const grid=document.createElement("div");grid.className="park-ai-snapshot-grid";rows.forEach(([label,value])=>{const span=document.createElement("span");span.textContent=label;const b=document.createElement("b");b.textContent=value;grid.append(span,b)});holder.appendChild(grid);snapshotBox.classList.add("on");
  }
  function applyResponse(data){
    if(data.message)addMessage(data.message,"system");
    if(data.sources)addSources(data.sources);
    const draft=data.draft||null;
    const card=data.snapshot||null;
    if(draft){pending=draft;renderSnapshot(card,draft);fillLegacyForm(draft)}
    if(card){pending=null;renderSnapshot(card,null)}
    confirm.disabled=!(draft&&data.confirmable===true);
    reject.disabled=!draft;
    if(data.status)setStatus(`状态：${data.status}${data.code?` · ${data.code}`:""}`);
  }
  async function post(action,payload){
    const response=await fetch("/api/park-paper/ai-chat",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({action,...payload})});
    let data={};try{data=await response.json()}catch(_error){data={message:"服务器返回了不可解析的响应。"}};
    if(!response.ok)throw new Error(data.message||data.error||`HTTP ${response.status}`);
    return data;
  }
  async function sendMessage(){
    const value=String(input.value||"").trim();if(!value)return;addMessage(value,"user");input.value="";send.disabled=true;setStatus("正在解析；不会自动下单……");
    try{applyResponse(await post("message",{message:value}))}catch(error){addMessage(`解析未完成：${error.message}`,"system");setStatus("没有创建或执行策略。可重试，或使用原来的手工 Paper 表单。")}finally{send.disabled=false}
  }
  async function confirmDraft(){
    if(!pending||!pending.draft_id||!pending.plan_digest)return;confirm.disabled=true;setStatus("正在重新核对最新行情、风险和 digest……");
    try{applyResponse(await post("confirm",{draft_id:pending.draft_id,plan_digest:pending.plan_digest}))}catch(error){addMessage(`确认未完成：${error.message}`,"system");setStatus("仍未执行；请重新读取 Draft 后再确认");confirm.disabled=false}
  }
  async function rejectDraft(){
    if(!pending||!pending.draft_id)return;reject.disabled=true;
    try{applyResponse(await post("reject",{draft_id:pending.draft_id}));pending=null;snapshotBox.classList.remove("on");snapshotBox.replaceChildren();confirm.disabled=true;setStatus("Draft 已拒绝且未保存为策略卡片。")}catch(error){addMessage(`拒绝未完成：${error.message}`,"system");reject.disabled=false}
  }
  async function hydrate(){
    // Browser-only static smoke servers do not expose the Paper API. Avoid a
    // needless 404 there; the real local Dashboard ports and public gateway
    // remain enabled.
    if(["127.0.0.1","localhost"].includes(location.hostname)&&!["8765","8766","8790"].includes(location.port))return;
    try{const response=await fetch("/api/park-paper/ai-chat",{credentials:"same-origin"});if(!response.ok)throw new Error(`HTTP ${response.status}`);const data=await response.json();if(data.pending_draft){pending=data.pending_draft;renderSnapshot(null,pending);confirm.disabled=!pending.confirmable;reject.disabled=false;setStatus("已恢复未过期 Draft；尚未下单。")}else if(Array.isArray(data.snapshots)&&data.snapshots.length){renderSnapshot(data.snapshots[data.snapshots.length-1],null);setStatus(`已读取 ${data.snapshots.length} 张策略 Snapshot；最新一张显示在上方。`)}}catch(error){setStatus(`AI 助手读取受限：${error.message}`)}
  }
  send.addEventListener("click",sendMessage);confirm.addEventListener("click",confirmDraft);reject.addEventListener("click",rejectDraft);input.addEventListener("keydown",event=>{if((event.metaKey||event.ctrlKey)&&event.key==="Enter"){event.preventDefault();sendMessage()}});hydrate();
})();
