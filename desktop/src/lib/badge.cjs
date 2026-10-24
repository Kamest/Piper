'use strict';
const digits = { '0':['111','101','101','101','111'], '1':['010','110','010','010','111'], '2':['111','001','111','100','111'], '3':['111','001','111','001','111'], '4':['101','101','111','001','001'], '5':['111','100','111','001','111'], '6':['111','100','111','101','111'], '7':['111','001','010','010','010'], '8':['111','101','111','101','111'], '9':['111','101','111','001','111'], '+':['000','010','111','010','000'] };

/** Draw a count onto a 24px BGRA tray bitmap without a canvas or a native dependency. */
function badge(bitmap, count, colour = '#ce9a2e') {
  const out = Buffer.from(bitmap);
  if (!count) return out;
  const rgb = colour.match(/[0-9a-f]{2}/gi)?.map(v => parseInt(v, 16)) || [206,154,46];
  const pixel = (x,y,c) => { const p=(y*24+x)*4; out[p]=c[2];out[p+1]=c[1];out[p+2]=c[0];out[p+3]=255; };
  for (let y=0;y<13;y++) for(let x=10;x<24;x++) if((x-17)**2+(y-6)**2<=44)pixel(x,y,rgb);
  const text=count<100?String(count):'99+';
  for(let i=0;i<text.length;i++) digits[text[i]].forEach((row,y)=>[...row].forEach((v,x)=>{if(v==='1')pixel((text.length===1?16:text.length===2?14:12)+i*4+x,y+4,[255,255,255]);}));
  return out;
}
module.exports={badge};
