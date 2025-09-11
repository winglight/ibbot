// Simple PnL calendar widget.
// Usage: PnLCalendar.init('containerId', [{date: '2024-01-01', pnl: 10}], {
//   locale: 'en-US', positiveColor: '#d4edda', negativeColor: '#f8d7da'
// })
class PnLCalendar {
  static init(containerId, data, options = {}) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = '';
    const positive = options.positiveColor || '#d4edda';
    const negative = options.negativeColor || '#f8d7da';
    const locale = options.locale || 'default';
    container.style.setProperty('--pnl-positive', positive);
    container.style.setProperty('--pnl-negative', negative);
    const map = {};
    const dates = [];
    (data || []).forEach(item => {
      if (!item.date) return;
      const d = new Date(item.date);
      if (isNaN(d)) return;
      const key = d.toISOString().slice(0,10);
      map[key] = Number(item.pnl || 0);
      dates.push(d);
    });
    if (dates.length === 0) return;
    dates.sort((a,b)=>a-b);
    let current = new Date(dates[0].getFullYear(), dates[0].getMonth(), 1);
    const last = new Date(dates[dates.length-1].getFullYear(), dates[dates.length-1].getMonth(), 1);
    while (current <= last) {
      this.renderMonth(container, current.getFullYear(), current.getMonth(), map, locale);
      current.setMonth(current.getMonth()+1);
    }
  }

  static renderMonth(container, year, month, map, locale) {
    const monthDiv = document.createElement('div');
    monthDiv.className = 'pnl-calendar-month';
    const title = document.createElement('h6');
    title.textContent = new Date(year, month).toLocaleDateString(locale, { month: 'long', year: 'numeric' });
    monthDiv.appendChild(title);
    const table = document.createElement('table');
    table.className = 'pnl-calendar-table';
    const head = document.createElement('tr');
    for (let i=0;i<7;i++) {
      const th = document.createElement('th');
      th.textContent = new Date(2020,5,i+1).toLocaleDateString(locale,{weekday:'short'});
      head.appendChild(th);
    }
    table.appendChild(head);
    const firstDay = new Date(year,month,1).getDay();
    const daysInMonth = new Date(year, month+1, 0).getDate();
    let row = document.createElement('tr');
    for (let i=0;i<firstDay;i++) row.appendChild(document.createElement('td'));
    for (let day=1; day<=daysInMonth; day++) {
      const td = document.createElement('td');
      const dateStr = `${year}-${String(month+1).padStart(2,'0')}-${String(day).padStart(2,'0')}`;
      const pnl = map[dateStr];
      td.textContent = day;
      if (pnl != null) {
        td.title = pnl.toFixed(2);
        td.className = 'pnl-calendar-cell ' + (pnl >= 0 ? 'positive' : 'negative');
      } else {
        td.className = 'pnl-calendar-cell';
      }
      row.appendChild(td);
      if ((firstDay + day) % 7 === 0 && day !== daysInMonth) {
        table.appendChild(row);
        row = document.createElement('tr');
      }
    }
    if (row.children.length > 0) {
      while (row.children.length < 7) row.appendChild(document.createElement('td'));
      table.appendChild(row);
    }
    monthDiv.appendChild(table);
    container.appendChild(monthDiv);
  }
}
window.PnLCalendar = PnLCalendar;
