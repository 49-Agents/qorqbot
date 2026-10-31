// ---------------------------------------------------------------- init
$("#handle").onclick = () => $("#navwrap").classList.toggle("pin");
document.addEventListener("click", e => { if (!$("#navwrap").contains(e.target)) $("#navwrap").classList.remove("pin"); });
setInterval(refreshSide, 12000);
loadBiz();
