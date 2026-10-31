// Запускает сценарии через браузерную версию (static/offline.js). Вход: JSON-файл (argv[2]), выход: JSON в stdout.
process.env.TZ = 'UTC';
const fs = require('fs');
const path = require('path');
const Offline = require(path.join(__dirname, '..', 'static', 'offline.js'));
const scenarios = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = [];
for (const sc of scenarios) {
  Offline.store.save([]);
  Offline.store.log = () => {};              // журнал для сравнения не нужен
  Offline.sess.pending = null; Offline.sess.last_id = null; Offline.sess.last_list = []; Offline.sess.choice = null;
  const ts = Date.parse(sc.now + ':00Z');
  const replies = sc.steps.map(t => { const r = Offline.handleText(t, ts); return [r.intent, r.reply]; });
  const items = Offline.store.all().map(r => [r.kind, r.title, r.due_date, r.due_time, r.status]);
  out.push({ replies, items });
}
process.stdout.write(JSON.stringify(out));
