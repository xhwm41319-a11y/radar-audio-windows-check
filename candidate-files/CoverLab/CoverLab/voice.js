// SPDX-License-Identifier: GPL-3.0-only
(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  let busy=false,available=false,mixedAvailable=false,polling=false,activeId=null,activeJob=null,reuseBlocked=false,eligibilityNotice='';
  async function json(response){const data=await response.json();if(!response.ok)throw Error(data.error||`HTTP ${response.status}`);return data;}
  const stageLabels={queued:'等待处理',pending:'等待处理',preparing:'准备输入',initializing:'初始化中',loading:'加载模型中',loading_model:'加载模型中',separating:'分离人声中',converting:'转换声线中',mixing:'混回伴奏中',finalizing:'整理结果中',publishing:'保存结果中',completed:'处理完成'};
  function inDurationRange(option,min,max){const duration=Number(option?.dataset.duration);return Number.isFinite(duration)&&duration>=min&&duration<=max;}
  function hasEligibleSelection(id,min,max){const select=$(id);return inDurationRange(select.options[select.selectedIndex],min,max);}
  function canStart(){return !reuseBlocked&&available&&hasEligibleSelection('voiceSource',1,600)&&hasEligibleSelection('voiceReference',3,15);}
  function updateEligibility(){
    $('voiceStart').disabled=busy||!canStart();
    if(busy)return;
    const source=$('voiceSource'),reference=$('voiceReference'),messages=[];
    if(![...source.options].some(option=>inDurationRange(option,1,600)))messages.push('没有符合 1–600 秒范围的输入音频，请导入合格素材。');
    else if(!hasEligibleSelection('voiceSource',1,600))messages.push('请选择 1–600 秒范围内的输入音频。');
    if(![...reference.options].some(option=>inDurationRange(option,3,15)))messages.push('没有 3–15 秒的参考音频，请导入合格素材。');
    else if(!hasEligibleSelection('voiceReference',3,15))messages.push('请选择 3–15 秒范围内的参考音频。');
    if(reuseBlocked)messages.push('上次引擎状态未确认，需人工核对后再使用；不会自动重跑。');
    if(messages.length){eligibilityNotice=messages.join(' ');$('voiceStatus').textContent=eligibilityNotice;}
    else{if(eligibilityNotice&&$('voiceStatus').textContent===eligibilityNotice)$('voiceStatus').textContent='已就绪：选择输入与3–15秒参考后生成；保持原音高。';eligibilityNotice='';}
  }
  function populateSelect(id,files,min,max){
    const select=$(id),previous=select.value;select.replaceChildren();
    for(const file of files){
      const duration=Number(file.duration);if(!Number.isFinite(duration)||duration<min||duration>max)continue;
      const option=document.createElement('option');option.value=file.id;option.dataset.duration=String(duration);option.textContent=`${file.name} · ${duration.toFixed(1)}秒`;select.append(option);
    }
    if([...select.options].some(option=>option.value===previous))select.value=previous;
    else if(select.options.length)select.selectedIndex=0;
  }
  function stageLabel(stage){return stageLabels[stage]||'本机处理中';}
  function restoreJobSelection(){if(!activeJob)return;for(const [control,key] of [['voiceSource','source_id'],['voiceReference','reference_id'],['voiceInputMode','input_mode'],['voiceSteps','steps'],['voiceProcessingMode','processing_mode']]){if(activeJob[key]!=null)$(control).value=String(activeJob[key]);}updateInputLabel();}
  function lockJobControls(locked){for(const id of ['voiceInputMode','voiceSource','voiceReference','voiceSteps','voiceProcessingMode'])$(id).disabled=locked;}
  function updateInputLabel(){
    const mixed=$('voiceInputMode').value==='mixed';
    $('voiceSourceLabel').textContent=mixed?'输入混合音频':'输入干声';
    $('voiceSource').setAttribute('aria-label',mixed?'输入混合音频':'输入干声');
  }
  function render(data){
    available=Boolean(data.capabilities?.timbre_conversion);
    mixedAvailable=Boolean(data.capabilities?.mixed_audio_conversion);
    $('voiceInputMode').querySelector('option[value="mixed"]').disabled=!mixedAvailable;
    if(!mixedAvailable&&$('voiceInputMode').value==='mixed'){$('voiceInputMode').value='dry';updateInputLabel();}
    $('voiceAvailable').textContent=available?'本地引擎已连接':'本地引擎不可用';
    const files=data.files||[];
    populateSelect('voiceSource',files,1,600);
    populateSelect('voiceReference',files,3,15);
    if(!$('voiceReference').value||$('voiceReference').value===$('voiceSource').value){const other=[...$('voiceReference').options].find(x=>x.value!==$('voiceSource').value);if(other)$('voiceReference').value=other.value;}
    restoreJobSelection();updateEligibility();
  }
  document.addEventListener('catalog-updated',e=>render(e.detail));
  async function poll(id){
    if(polling)return;polling=true;let again=false;
    try{
      const job=await json(await fetch(`/api/voice/${id}`));activeId=id;reuseBlocked=reuseBlocked||Boolean(job.engine_reuse_blocked);
      if(['running','cancelling'].includes(job.status)){
        busy=true;activeJob=job;restoreJobSelection();lockJobControls(true);$('voiceStart').disabled=true;$('voiceCancel').hidden=false;$('voiceCancel').disabled=job.status==='cancelling';
        $('voiceStatus').textContent=job.status==='cancelling'?'正在停止引擎，请勿重复提交。':`${job.stage?stageLabel(job.stage):'本机转换中'} · 已用${job.wall_elapsed_seconds||0}秒 · 最长运行预算${job.timeout_seconds}秒（不是预计完成时间）`;
        again=true;
      }else{
        busy=false;activeJob=null;lockJobControls(false);$('voiceStart').disabled=!canStart();$('voiceCancel').hidden=true;activeId=null;
        if(job.status!=='completed'){$('voiceStatus').textContent=job.status==='cancelled'?'已停止，没有发布转换结果。':`未完成：${job.error}`;}
        else{
          $('voiceStatus').textContent=`${job.input_mode==='mixed'?'混合音频转换':'干声转换'}完成 · ${job.device.toUpperCase()} · ${job.elapsed_seconds.toFixed(1)}秒`;
          $('voicePreview').src=job.file.url;$('voicePreview').hidden=false;
          $('voiceDownload').href=job.file.url;$('voiceDownload').download=job.file.name;$('voiceDownload').hidden=false;document.dispatchEvent(new CustomEvent('voice-output',{detail:job.file}));$('refreshButton').click();
        }
        if(reuseBlocked)$('voiceStatus').textContent+=' · 上次引擎状态未确认，需人工核对后再使用；不会自动重跑。';
      }
    }catch(e){
      // Lost observation is not a terminal job: retain busy/cancel state and retry the SAME id.
      $('voiceStatus').textContent=`暂时无法读取进度：${e.message}。正在重新连接同一任务。`;again=true;
    }finally{polling=false;if(again)setTimeout(()=>poll(id),1500);}
  }
  $('voiceCancel').addEventListener('click',async()=>{
    if(!activeId)return;$('voiceCancel').disabled=true;
    try{await json(await fetch(`/api/voice/${activeId}/cancel`,{method:'POST'}));$('voiceStatus').textContent='正在停止引擎…';}
    catch(e){$('voiceCancel').disabled=false;$('voiceStatus').textContent=`停止请求未确认：${e.message}，继续核对原任务。`;}
  });
  $('voiceInputMode').addEventListener('change',()=>{updateInputLabel();updateEligibility();});
  $('voiceSource').addEventListener('change',updateEligibility);
  $('voiceReference').addEventListener('change',updateEligibility);
  updateInputLabel();
  $('voiceStart').addEventListener('click',async()=>{
    if(busy||!canStart())return;busy=true;lockJobControls(true);$('voiceStart').disabled=true;$('voicePreview').hidden=true;$('voiceDownload').hidden=true;
    $('voiceStatus').textContent='正在提交本地任务…';
    (async()=>{try{const job=await json(await fetch('/api/voice',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:$('voiceSource').value,reference_id:$('voiceReference').value,steps:Number($('voiceSteps').value),input_mode:$('voiceInputMode').value,processing_mode:$('voiceProcessingMode').value})}));activeId=job.id;poll(job.id);}
      catch(e){$('voiceStatus').textContent=`提交状态待核对：${e.message}。不会自动重发。`;await discover();}})();
  });
  async function discover(){
    lockJobControls(true);
    try{const list=await json(await fetch('/api/voice'));reuseBlocked=Boolean(list.engine_reuse_blocked);const job=list.jobs.find(j=>j.status==='recovery_required')||list.jobs.find(j=>['running','cancelling'].includes(j.status))||list.jobs[0];if(job){busy=true;poll(job.id);}else{busy=false;activeJob=null;lockJobControls(false);updateEligibility();}}
    catch(e){busy=true;$('voiceStart').disabled=true;$('voiceStatus').textContent='暂时无法核对原任务，正在重连；不会重复生成。';setTimeout(discover,1500);}
  }
  discover();
})();
