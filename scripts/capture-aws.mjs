import {chromium} from 'playwright';
import assert from 'node:assert/strict';
import {readFile, writeFile} from 'node:fs/promises';
import path from 'node:path';

const evidence=JSON.parse(await readFile('media/aws-evidence.json','utf8'));
const base=evidence.endpoint;
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH||undefined});
const watchdog=setTimeout(()=>browser.close().finally(()=>process.exit(1)),300000);
try{
  const context=await browser.newContext({viewport:{width:1920,height:1080},deviceScaleFactor:1});
  if(process.env.GATE_RESULTS){
    const results=JSON.parse(await readFile(process.env.GATE_RESULTS,'utf8'));
    const checks=[],errors=[];
    const page=await context.newPage();
    page.on('pageerror',e=>errors.push(e.message));
    for(const clip of results.clips){
      const url=new URL(clip.report_url);
      await page.goto(base+'/?report='+encodeURIComponent(url.pathname),{waitUntil:'networkidle'});
      await page.waitForFunction(()=>document.getElementById('filename').textContent!=='Load a report to begin');
      assert(await page.locator('#error').isHidden());
      assert.equal(await page.locator('#cloud').textContent(),'AWS: processed');
      assert.equal(await page.locator('#caption-status').textContent(),'DNN CAPTIONS ACTIVE');
      const report=await (await page.request.get(clip.report_url)).json();
      assert.equal(Number(await page.locator('#issue-count').textContent()),report.issues.length);
      await page.locator('#video-file').setInputFiles(path.join(process.env.GATE_CLIPS||'work/gate',clip.file));
      await page.locator('#video').evaluate(async v=>{
        if(v.readyState<2)await new Promise((resolve,reject)=>{v.addEventListener('loadeddata',resolve,{once:true});v.addEventListener('error',()=>reject(Error('video decode failed')),{once:true});});
        v.currentTime=2.5;await new Promise(r=>v.addEventListener('seeked',r,{once:true}));
        await v.play();await new Promise(r=>v.requestVideoFrameCallback(r));v.pause();
      });
      for(const id of ['csv-link','sheet-link','json-link'])assert.equal((await page.request.get(await page.locator('#'+id).getAttribute('href'))).status(),200);
      if(report.issues.length){
        await page.locator('#findings .finding').first().click();
        await page.locator('#filter').selectOption('error');
        assert.equal(await page.locator('#findings .finding.review').count(),0);
        await page.locator('#filter').selectOption('all');
      }
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      if(clip.file==='studio-short-defects.mp4')await page.screenshot({path:'media/realism-live.png'});
      await page.setViewportSize({width:390,height:844});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await page.setViewportSize({width:1920,height:1080});
      checks.push({file:clip.file,report_url:clip.report_url,issues:report.issues.length,decode_seek_download_filter_desktop_mobile:'PASS'});
    }
    assert.deepEqual(errors,[]);
    await writeFile(path.join(path.dirname(process.env.GATE_RESULTS),'browser.json'),JSON.stringify({checked_at:new Date().toISOString(),checks,page_errors:errors},null,2)+'\n');
    console.log(`PASS: ${checks.length} live AWS reports, original/repaired video decode and seek, evidence downloads, filter, desktop/mobile; no page errors.`);
  }else if(process.env.METRICS_ONLY){
    const proof=await context.newPage();
    const escape=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    const data=evidence.cloudwatch_metrics.MetricDataResults;
    const rows=data.map(m=>`<tr><td>${escape(m.Id)}</td><td>${escape(m.Values.join(', '))}</td><td>${escape(m.Timestamps.map(t=>new Date(t).toISOString()).join(' / '))}</td></tr>`).join('');
    const total=id=>data.find(m=>m.Id===id).Values.reduce((a,b)=>a+b,0);
    const maximum=Math.max(...data.find(m=>m.Id==='duration').Values)/1000;
    await proof.setContent(`<!doctype html><meta charset="utf-8"><style>body{background:#0a1016;color:#edf3f6;font:19px system-ui;padding:65px 85px;margin:0}h1{font-size:43px}p,small{color:#a3b5c4}header{color:#79e6c6;letter-spacing:3px;font-size:15px}.stats{display:flex;gap:25px;margin:35px 0}.stats div{background:#11212a;border:1px solid #32544f;border-radius:10px;padding:25px;width:30%}strong{font-size:45px;display:block;color:#79e6c6}small{display:block;margin-top:8px}table{width:100%;border-collapse:collapse;margin:25px 0;font:17px monospace}td,th{text-align:left;border-bottom:1px solid #30414e;padding:18px 14px}td:last-child{font-size:14px}pre{background:#11212a;padding:25px;border-radius:10px;font:16px/1.6 monospace;white-space:pre-wrap;overflow-wrap:anywhere}footer{font-size:14px;color:#a3b5c4}</style><header>SHORTS QC / CLOUDWATCH API EVIDENCE</header><h1>Real analysis requests. Measured execution.</h1><p>${escape(evidence.refreshed_at||evidence.captured_at)} · ap-southeast-2 · Actual CLI responses rendered for readability</p><div class="stats"><div><strong>${total('invocations')}</strong><small>worker invocations · sum of 60s buckets</small></div><div><strong>${total('errors')}</strong><small>Lambda Errors · sum of 60s buckets</small></div><div><strong>${maximum.toFixed(3)}s</strong><small>largest recorded Duration · maximum per 60s bucket</small></div></div><table><tr><th>CloudWatch metric</th><th>Recorded values</th><th>UTC metric timestamps</th></tr>${rows}</table><h2>Live Lambda configuration</h2><pre>${escape(evidence.lambda_configuration.map(c=>JSON.stringify({function:c.FunctionName,memory_mib:c.MemorySize,timeout_seconds:c.Timeout,reserved_concurrency:c.ReservedConcurrentExecutions,state:c.State})).join("\n"))}</pre><footer>Source: aws cloudwatch get-metric-data (AWS/Lambda, shorts-qc-analyze) and aws lambda get-function-configuration / get-function-concurrency. Full timestamps and responses: media/aws-evidence.json.</footer>`);
    await proof.screenshot({path:'media/aws-metrics.png'});
    console.log('CloudWatch metrics screenshot captured from recorded CLI responses.');
  }else{
  const page=await context.newPage();const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base,{waitUntil:'networkidle'});
  await page.locator('#aws-upload').waitFor();
  await page.locator('#findings .finding').first().waitFor();
  assert.equal(await page.locator('#cloud').textContent(),'AWS: processed');
  assert.equal(await page.locator('#caption-status').textContent(),'DNN CAPTIONS ACTIVE');
  await page.locator('#video').evaluate(async v=>{
    if(v.readyState<1)await new Promise((resolve,reject)=>{v.addEventListener('loadedmetadata',resolve,{once:true});v.addEventListener('error',()=>reject(Error('demo decode failed')),{once:true});});
    v.currentTime=9.5;await new Promise(r=>v.addEventListener('seeked',r,{once:true}));
    await v.play();await new Promise(r=>v.requestVideoFrameCallback(r));v.pause();
  });
  await page.screenshot({path:'media/aws-viewer.png'});
  for(const id of ['csv-link','sheet-link','json-link'])assert.equal((await page.request.get(await page.locator(`#${id}`).getAttribute('href'))).status(),200);
  const range=await page.request.get(base+'/demo.mp4',{headers:{Range:'bytes=0-99'}});
  assert.equal(range.status(),206);assert.equal((await range.body()).length,100);
  await page.locator('#upload-video').setInputFiles('media/demo.mp4');
  await page.waitForFunction(()=>document.getElementById('upload-status').textContent.startsWith('AWS analysis complete')||!document.getElementById('error').hidden,{},{timeout:240000});
  assert.match(await page.locator('#upload-status').textContent(),/^AWS analysis complete/,await page.locator('#error').textContent());
  assert.equal(await page.locator('#cloud').textContent(),'AWS: processed');
  assert(await page.locator('#video').getAttribute('src').then(x=>x.startsWith('blob:')));
  await page.locator('#filter').selectOption('error');
  assert.equal(await page.locator('#findings .finding.review').count(),0);
  await page.locator('#findings .finding').first().click();
  await page.waitForFunction(()=>document.getElementById('video').readyState>=2);
  await page.screenshot({path:'media/aws-upload-complete.png'});
  const shared=await context.newPage();await shared.goto(page.url(),{waitUntil:'networkidle'});
  await shared.locator('#findings .finding').first().waitFor();
  assert.equal(await shared.locator('#cloud').textContent(),'AWS: processed');
  assert.equal(await shared.locator('#video').getAttribute('src'),null);
  const mobile=await context.newPage({viewport:{width:390,height:844}});
  await mobile.setViewportSize({width:390,height:844});
  await mobile.goto(base,{waitUntil:'networkidle'});await mobile.locator('#findings .finding').first().waitFor();
  assert(await mobile.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.deepEqual(errors,[]);
  const escape=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  const runs=evidence.runs.map(r=>`<div class="run"><h2>${escape(r.clip)} <span>COMPLETE</span></h2><div class="metrics"><strong>${r.frames} <small>frames inspected</small></strong><strong>${r.analysis_seconds.toFixed(3)}s <small>OpenCV + DNN</small></strong><strong>${r.upload_to_report_seconds.toFixed(3)}s <small>upload → report</small></strong><strong>HTTP ${r.replay_http_status}<small>overwrite rejected</small></strong></div><p>Lambda request ID <code>${r.request_id}</code></p><p>Job <code>${r.job_id}</code></p></div>`).join('');
  const logs=evidence.cloudwatch_logs.filter(e=>/REPORT RequestId|"status": "complete"/.test(e.message)).map(e=>e.message.trim()).join('\n');
  await shared.setViewportSize({width:1920,height:1080});
  await shared.setContent(`<!doctype html><html><meta charset="utf-8"><style>body{margin:0;background:#0a1016;color:#edf3f6;font:18px system-ui;padding:58px 90px}header{color:#79e6c6;letter-spacing:3px;font-size:15px}h1{font-size:45px;margin:16px 0}p{color:#adbecb}code{font:16px monospace;color:#d6e3ed}.meta{padding:15px 22px;border:1px solid #32544f;background:#102523;border-radius:8px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}.run{background:#111c27;border:1px solid #2d4050;border-radius:12px;padding:22px}h2{font-size:22px;margin-top:0}h2 span{float:right;font-size:14px;color:#79e6c6}.metrics{display:flex;justify-content:space-between;margin:24px 0}strong{font-size:28px}small{display:block;font-size:12px;color:#94a4b3;font-weight:400;margin-top:5px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.7 monospace;background:#111c27;padding:20px;border-radius:8px;max-height:285px;overflow:hidden}footer{color:#94a4b3;font-size:13px}</style><header>SHORTS QC / REAL AWS WORKLOAD</header><h1>From private upload to verified report.</h1><p>Recorded AWS API responses · ${escape(evidence.refreshed_at||evidence.captured_at)} · ap-southeast-2 · CLI evidence rendered for readability</p><p class="meta">Free plan ${escape(evidence.free_plan.accountPlanStatus)} · Credits at capture: $${evidence.free_plan.accountPlanRemainingCredits.amount} · Analyzer: 2048 MiB / 180s · 1 reserved worker · Signed uploads: 25 MiB / 200 total</p><div class="grid">${runs}</div><h2 style="margin-top:30px">CloudWatch execution records</h2><pre>${escape(logs)}</pre><footer>Source: aws cloudformation describe-stacks · aws lambda get-function-configuration · aws logs filter-log-events · aws freetier get-account-plan-state. Full records: media/aws-evidence.json. These are real AWS executions, not local simulation.</footer></html>`);
  await shared.screenshot({path:'media/aws-evidence.png'});
  console.log('PASS: public AWS viewer, S3 signed browser upload, real analysis, polling, share URL, video decode/seek/206, evidence downloads, filtering, mobile overflow; 1920x1080 captures saved.');
  }
  await context.close();
}finally{clearTimeout(watchdog);await browser.close();}
