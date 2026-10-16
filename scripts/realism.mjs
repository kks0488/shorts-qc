// Original creator-style shorts assembled from our own app captures. Run on pilot.
import {chromium} from 'playwright';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import path from 'node:path';

const out = path.resolve(process.argv[2] || 'work/realism');
await mkdir(out, {recursive:true});
const browser = await chromium.launch({headless:true, executablePath:process.env.CHROMIUM_PATH || undefined});
try {
  const page = await browser.newPage({viewport:{width:720,height:1280}, deviceScaleFactor:1});
  const sources = [
    {name:'review-short', image:'media/aws-viewer.png', title:'Check the frame.\nKeep the story.', label:'THE CREATOR WORKFLOW', detail:'Review your video\nbefore you publish.', color:'#1d7665'},
    {name:'editor-short', image:'media/viewer-finding.png', title:'Find the moment.\nReview the fix.', label:'THE EDITOR WORKFLOW', detail:'Find the frame.\nChoose the next step.', color:'#64543e'},
    {name:'studio-short', image:'media/aws-upload-complete.png', title:'Keep it moving.\nCheck the light.', label:'THE STUDIO WORKFLOW', detail:'A clear export.\nA careful final check.', color:'#395f86'},
  ];
  const manifest = {schema_version:'1.0', fps:24, duration:12, width:720, height:1280,
    provenance:'Original motion design composed from our own app screen captures; no stock footage or external artwork.',
    ground_truth:'Exact encoded frame intervals; all other frames are negatives for the six evaluated checks. Three source styles, not a population benchmark.',
    clips:[]};
  for (const item of sources) {
    const png = (await readFile(item.image)).toString('base64');
    await page.setContent(`<!doctype html><style>
      *{box-sizing:border-box}body{margin:0;width:720px;height:1280px;overflow:hidden;background:#e2dfd8;color:#1e2928;font-family:Arial,sans-serif}
      .light{position:absolute;inset:0;background:radial-gradient(ellipse at 20% 30%,#fff9 0%,transparent 65%)}
      .line{position:absolute;width:800px;height:800px;border:1px solid #bdc7bd;border-radius:50%;left:100px;top:520px;transform:rotate(-20deg)}
      main{position:absolute;left:70px;top:170px;width:500px}.label{font-size:17px;letter-spacing:2px;color:${item.color};font-weight:bold}
      h1{font-size:49px;line-height:1.1;letter-spacing:-2px;margin:23px 0 40px;white-space:pre-line}
      .window{background:#f6f7f3;border:1px solid #bfc8c2;border-radius:18px;padding:12px;box-shadow:12px 30px 35px #324b4233;transform:perspective(1000px) rotateY(-4deg)}
      .dots{height:25px;display:flex;gap:7px;align-items:start}.dots i{width:7px;height:7px;border-radius:50%;background:#99ada3}
      img{display:block;width:100%;border-radius:7px}h2{font-size:34px;line-height:1.25;letter-spacing:-1px;margin-top:42px;white-space:pre-line}
      .rule{height:5px;width:76px;background:${item.color};margin-top:28px}
      .footer{font-size:18px;margin-top:22px;color:#50615a}
    </style><div class="light"></div><div class="line"></div><main><div class="label">${item.label}</div><h1>${item.title}</h1><div class="window"><div class="dots"><i></i><i></i><i></i></div><img src="data:image/png;base64,${png}"></div><h2>${item.detail}</h2><div class="rule"></div><div class="footer">A practical workflow, one step at a time.</div></main>`);
    await page.screenshot({path:path.join(out,`${item.name}.png`)});
    // A slow camera move matches common creator screen-demo shorts. No noise texture
    // is added to improve detector scores; intentional holds remain reviewable.
    const clean = `${item.name}-clean.mp4`, defect = `${item.name}-defects.mp4`;
    const run = args => {
      const r=spawnSync('ffmpeg', ['-hide_banner','-loglevel','error','-y',...args], {encoding:'utf8'});
      if(r.status!==0) throw Error(r.stderr);
    };
    run(['-loop','1','-i',path.join(out,`${item.name}.png`),'-vf',
      "scale=1440:2560,zoompan=z='1.02+0.00014*on':x='iw/2-iw/zoom/2+12*sin(on/35)':y='ih/2-ih/zoom/2':d=288:s=720x1280:fps=24",
      '-frames:v','288','-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p',path.join(out,clean)]);
    run(['-i',path.join(out,clean),'-filter_complex',
      (item.name==='studio-short'?"[0:v]split[a][b];[a][b]freezeframes=first=144:last=179:replace=143,lutrgb=r=val*0.15:g=val*0.15:b=val*0.15:enable='gte(t,10.5)*lt(t,11.5)',":"[0:v]")+
      "gblur=sigma=22:enable='gte(t,2)*lt(t,3)',drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='gte(t,5)*lt(t,5.5)',drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='Keep this caption in view':fontsize=38:fontcolor=white:box=1:boxcolor=black:x=70:y=1253:enable='gte(t,8)*lt(t,10)'",
      '-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p',path.join(out,defect)]);
    for(const [file, damaged] of [[clean,false],[defect,true]]) manifest.clips.push({file, style:item.name, reference:damaged?clean:undefined,
      defects:damaged?{blur:[[2,3]],black:[[5,5.5]],caption_safe_zone:[[8,10]],caption_clipped:[[8,10]],...(item.name==='studio-short'?{frozen:[[6,7.5]],dark:[[10.5,11.5]]}:{})}:{},
      intentional:'Slow screen-demo camera motion and graphic background; stillness is intentional, and screen text is not a spoken caption.'});
  }
  await writeFile(path.join(out,'manifest.json'),JSON.stringify(manifest,null,2)+'\n');
  console.log(`Rendered ${manifest.clips.length} original 720x1280 shorts in ${out}`);
} finally {await browser.close();}
