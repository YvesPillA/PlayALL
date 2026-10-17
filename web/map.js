const colors = {"iron-ore":"#90a2b6","copper-ore":"#c58b55",coal:"#435a66",stone:"#b6ad89",wood:"#44846e","uranium-ore":"#96b944","crude-oil":"#594d69"};
let scale = 1;
let current = null;

export function zoomMap(delta) {
  scale = delta === 0 ? 1 : Math.max(.55, Math.min(3, scale * delta));
  renderMap(current);
}

export function renderMap(state) {
  current = state;
  const canvas = document.querySelector("#world-map");
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (!width || !height) return;
  const dpr = Math.min(devicePixelRatio || 1, 2);
  if (canvas.width !== Math.round(width*dpr) || canvas.height !== Math.round(height*dpr)) {
    canvas.width = Math.round(width*dpr); canvas.height = Math.round(height*dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.fillStyle = "#e8eff3"; ctx.fillRect(0,0,width,height);
  if (!state) {
    ctx.strokeStyle = "#dce6ed"; ctx.lineWidth = 1;
    for (let x=width%32; x<width; x+=32) {ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,height);ctx.stroke();}
    for (let y=height%32; y<height; y+=32) {ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(width,y);ctx.stroke();}
    return;
  }
  const p = state.position || {x:0,y:0};
  const factor = Math.min(width/210,height/155)*scale;
  const point = (x,y) => [width/2+(x-p.x)*factor,height/2+(y-p.y)*factor];
  if (state.map?.tiles?.length) {
    const map = state.map;
    for (let i=0;i<map.tiles.length;i++) {
      const [x,y] = point(map.x+(i%map.width)*map.step,map.y+Math.floor(i/map.width)*map.step);
      ctx.fillStyle=map.tiles[i] ? "#a9c9db" : "#d2e1d3";
      ctx.fillRect(x,y,map.step*factor+.6,map.step*factor+.6);
    }
  } else {
    ctx.fillStyle = "#d2e1d3"; ctx.fillRect(0,0,width,height);
  }
  ctx.strokeStyle="#779c8b16"; ctx.lineWidth=1;
  for(let n=-256;n<=256;n+=16){
    let [x]=point(n,0);ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,height);ctx.stroke();
    let [,y]=point(0,n);ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(width,y);ctx.stroke();
  }
  if(Array.isArray(state.map?.resources)){
    for(const [rx,ry,name] of state.map.resources){
      const [x,y]=point(rx,ry);ctx.fillStyle=colors[name]||"#9aa68a";ctx.globalAlpha=.83;
      ctx.fillRect(x-1.8*factor,y-1.8*factor,3.6*factor,3.6*factor);
    }
    ctx.globalAlpha=1;
  } else {
    for(const resource of state.resources || []){
      const [x,y]=point(resource.x,resource.y);ctx.fillStyle=colors[resource.name]||"#9aa68a";
      ctx.beginPath();ctx.ellipse(x,y,11*factor,7*factor,-.4,0,Math.PI*2);ctx.fill();
    }
  }
  ctx.font="10px 'Microsoft YaHei UI',sans-serif";
  const names={"iron-ore":"铁矿","copper-ore":"铜矿",coal:"煤矿",stone:"石矿",wood:"树林","uranium-ore":"铀矿","crude-oil":"原油"};
  for(const resource of state.resources || []){
    const [x,y]=point(resource.x,resource.y);
    if(x<30||x>width-30||y<28||y>height-28)continue;
    const label=names[resource.name]||resource.name;
    const tw=ctx.measureText(label).width;
    ctx.fillStyle="#f8fbf1dd";ctx.fillRect(x-tw/2-6,y+8,tw+12,19);
    ctx.fillStyle="#496453";ctx.textAlign="center";ctx.fillText(label,x,y+21);
  }
  for(const building of state.buildings||[]){
    const [x,y]=point(building.x,building.y);
    const size=Math.max(7,4*factor);
    ctx.fillStyle="#fff8d6";ctx.strokeStyle="#936d37";ctx.lineWidth=1.5;
    ctx.fillRect(x-size/2,y-size/2,size,size);ctx.strokeRect(x-size/2,y-size/2,size,size);
    ctx.fillStyle="#ad7b39";ctx.fillRect(x-1.5,y-1.5,3,3);
  }
  const [bx,by]=point(p.x,p.y);
  ctx.fillStyle="#2558dd16";ctx.beginPath();ctx.arc(bx,by,22,0,Math.PI*2);ctx.fill();
  ctx.strokeStyle="#2558dd44";ctx.lineWidth=1;ctx.beginPath();ctx.arc(bx,by,22,0,Math.PI*2);ctx.stroke();
  ctx.fillStyle="#2558dd";ctx.strokeStyle="#fff";ctx.lineWidth=2.5;ctx.beginPath();ctx.arc(bx,by,7,0,Math.PI*2);ctx.fill();ctx.stroke();
  ctx.textAlign="center";ctx.fillStyle="#173e6e";ctx.font="bold 10px 'Microsoft YaHei UI',sans-serif";ctx.fillText("AI 工程师",bx,by-30);
  ctx.textAlign="left";ctx.fillStyle="#4d6e64";ctx.font="9px 'Microsoft YaHei UI',sans-serif";
  const ruler=20*factor;ctx.fillRect(20,height-24,ruler,1);ctx.fillRect(20,height-27,1,4);ctx.fillRect(20+ruler,height-27,1,4);ctx.fillText("20 格",20,height-32);
}

window.addEventListener("resize",()=>renderMap(current));
