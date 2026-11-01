/* Decorative banking-icon watermark for the admin sign-in screen. Same icons and look as the public landing page:
   small, faint, floating gently, scattered in clusters and gaps, and never over the sign-in card. */
(function(){
 var L=document.querySelector('.wm'),B=L&&L.querySelector('.wm-box'),K=document.querySelector('.signbox'),timer=0;
 if(!B||!K)return;
 var NAMES=[].map.call(L.querySelectorAll('symbol'),function(s){return s.id}),
     io=('IntersectionObserver' in window)?new IntersectionObserver(function(es){es.forEach(function(e){e.target.classList.toggle('live',e.isIntersecting)})}):null;
 function seedOf(s){var h=2166136261;for(var i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619)}return h>>>0}
 function mulberry(a){return function(){a|=0;a=a+0x6D2B79F5|0;var t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296}}
 function makeNoise(r){var g=[],i;for(i=0;i<4096;i++)g.push(r());function p(a,b){return g[((b&63)<<6)+(a&63)]}
  return function(x,y){var gx=Math.floor(x),gy=Math.floor(y),fx=x-gx,fy=y-gy,u=fx*fx*(3-2*fx),v=fy*fy*(3-2*fy);
   return(p(gx,gy)*(1-u)+p(gx+1,gy)*u)*(1-v)+(p(gx,gy+1)*(1-u)+p(gx+1,gy+1)*u)*v}}
 function build(){
  if(io)io.disconnect();
  B.textContent='';
  var W=document.documentElement.clientWidth,H=window.innerHeight,r=K.getBoundingClientRect(),PAD=44;
  if(!NAMES.length||W<240||H<240)return;
  var rnd=mulberry(seedOf('admin-signin')),noise=makeNoise(rnd),MAX=Math.min(70,Math.round(W*H/9000)),placed=0,t,made=[],pts=[],frag=document.createDocumentFragment();
  for(t=0;t<MAX*45&&placed<MAX;t++){
   var x=rnd()*W,y=rnd()*H,n=noise(x/380,y/380);                      /* slow noise: denser clusters and emptier patches */
   if(n<.2||(n<.45&&rnd()>(n-.2)/.25))continue;
   var s=Math.round(14+Math.pow(rnd(),1.7)*11),h=s/2+8;
   if(x-h<0||x+h>W||y-h<0||y+h>H)continue;
   if(x+h>r.left-PAD&&x-h<r.right+PAD&&y+h>r.top-PAD&&y-h<r.bottom+PAD)continue;   /* keep clear of the card */
   var gap=18+(1-n)*46,ok=true,q;
   for(q=0;q<pts.length;q++){var dx=pts[q][0]-x,dy=pts[q][1]-y,mn=(pts[q][2]+s)/2+gap;if(dx*dx+dy*dy<mn*mn){ok=false;break}}
   if(!ok)continue;
   pts.push([x,y,s]);placed++;
   var op=Math.min(.3,(.15+rnd()*.13)*(.75+.5*n)),e=document.createElement('i');
   e.style.cssText='left:'+(x-s/2).toFixed(1)+'px;top:'+(y-s/2).toFixed(1)+'px;width:'+s+'px;height:'+s+'px;--o:'+op.toFixed(2)+';--r:'+Math.round((rnd()-.5)*50)+'deg;--dx:'+((rnd()-.5)*6).toFixed(1)+'px;--dy:-'+(4+rnd()*5).toFixed(1)+'px;--t:'+(18+rnd()*14).toFixed(1)+'s;--d:-'+(rnd()*20).toFixed(1)+'s';
   e.innerHTML='<svg><use href="#'+NAMES[Math.floor(rnd()*NAMES.length)]+'"/></svg>';
   frag.appendChild(e);made.push(e);
  }
  B.appendChild(frag);
  if(io)made.forEach(function(e){io.observe(e)}); else made.forEach(function(e){e.classList.add('live')});
 }
 function schedule(){clearTimeout(timer);timer=setTimeout(function(){requestAnimationFrame(build)},200)}
 build();addEventListener('resize',schedule);addEventListener('load',schedule);
})();
