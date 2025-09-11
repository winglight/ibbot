(function(){"use strict";
// Lightweight Notifications Manager that proxies app's notification methods
const NAMES=[
  "addNotification","clearNotifications","updateNotificationPanel","removeNotification",
  "showNotificationSettings","populateBasicSettings","populateModuleSettings","getAvailableModules",
  "saveNotificationSettings","testTradingReminder","checkTradingReminderStatus","testNotification",
  "loadNotificationHistory","renderNotificationHistoryRows","getNotificationStatusBadge","getChannelIcon",
  "filterNotificationHistory","showNotificationHistory"
];

class NotificationsManager{constructor(app,orig){this.app=app;this.orig=orig||{};}forward(n,...a){const f=this.orig[n];if(typeof f==="function"){return f(...a);}if(console&&console.warn){console.warn(`[Notifications] Missing original method: ${n}`);}return void 0;}
  showNotificationHistory(){try{const load=this.app.loadNotificationHistory||this.orig.loadNotificationHistory; if(typeof load==="function"){load();}
    const table=document.getElementById("notificationHistoryTable")||document.getElementById("notificationHistoryBody");
    if(table&&table.scrollIntoView){table.scrollIntoView({behavior:"smooth",block:"center"}); const card=table.closest&&table.closest('.card')||table; const prev=card.style.backgroundColor; card.style.transition='background-color .6s ease'; card.style.backgroundColor='rgba(255,243,205,.6)'; setTimeout(()=>{card.style.backgroundColor=prev||'';},800);} }catch(e){console.error("showNotificationHistory error:",e);} }
}

// Public hook: called from app.js after app instance is created
window.__attachNotifications=function(app){try{if(!app||typeof app!=="object")return; const orig={}; NAMES.forEach(n=>{if(typeof app[n]==="function"){orig[n]=app[n].bind(app);}});
  const mgr=new NotificationsManager(app,orig); app.notificationsManager=mgr;
  NAMES.forEach(n=>{ if(n in mgr && typeof mgr[n]==="function"){ app[n]=(...args)=>mgr[n](...args); } else { app[n]=(...args)=>mgr.forward(n,...args); } });
  // Initial bootstrap: load history once if placeholder is visible
  const tbody=document.getElementById("notificationHistoryBody"); const should=/Loading notification history/i.test((tbody&&tbody.textContent)||"");
  if(should&&typeof app.loadNotificationHistory==="function"){ setTimeout(()=>{ try{app.loadNotificationHistory();}catch(_){} },0); }
}catch(err){console.error("Failed to attach NotificationsManager:",err);} };
})();