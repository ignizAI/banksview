/* Phone fields: only digits can be typed or pasted, so a number never contains spaces, dashes or brackets. */
(function(){
 function clean(el){var v=el.value,start=el.selectionStart,w=v.replace(/\D/g,'');if(w!==v){el.value=w;try{el.setSelectionRange(start-(v.length-w.length),start-(v.length-w.length))}catch(e){}}}
 document.querySelectorAll('input[data-digits]').forEach(function(el){
  el.addEventListener('input',function(){clean(el)});
  el.addEventListener('paste',function(){setTimeout(function(){clean(el)},0)});
  el.addEventListener('keydown',function(e){if(e.key===' ')e.preventDefault()});
  clean(el);
 });
})();
/* Name fields: letters only. */
(function(){
 document.querySelectorAll('input[data-letters]').forEach(function(el){
  function clean(){var v=el.value,w=v.replace(/[^\p{L}]/gu,'');if(w!==v)el.value=w}
  el.addEventListener('input',clean);el.addEventListener('paste',function(){setTimeout(clean,0)});clean();
 });
})();
