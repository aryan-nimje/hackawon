require('dotenv').config();
const express = require('express'), path = require('path'), { Pool } = require('pg');
const url = process.env.DATABASE_URL;
if (!url || url.startsWith('YOUR_')) console.warn('⚠  Set DATABASE_URL in .env (see .env.example)');
const pool = new Pool({ connectionString: url });
const app = express();
app.use(express.json({ limit: '20kb' }));
app.use(express.static(path.join(__dirname, 'public')));
app.get('/report', (_q, r) => r.sendFile(path.join(__dirname, 'public/report.html')));

const NEEDS = ['rescue', 'medical', 'shelter', 'food_water', 'other'];
const URG = ['low', 'moderate', 'high', 'critical'];
const VULN = ['children', 'elderly', 'limited_mobility', 'pregnant', 'medical_needs'];
const clean = s => String(s).replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, '').replace(/[<>]/g, '').trim();

function validate(b) {
  const e = {}; b = b || {};
  const text = typeof b.text === 'string' ? clean(b.text) : '';
  if (!text) e.text = 'Please describe what is happening.';
  else if (text.length > 500) e.text = 'Description must be 500 characters or fewer.';
  const location = typeof b.location === 'string' ? clean(b.location).slice(0, 300) : '';
  if (!location) e.location = 'Please choose a location.';
  const lat = Number(b.lat), lng = Number(b.lng);
  if (b.lat == null || !Number.isFinite(lat) || lat < -90 || lat > 90) e.lat = 'Invalid latitude.';
  if (b.lng == null || !Number.isFinite(lng) || lng < -180 || lng > 180) e.lng = 'Invalid longitude.';
  if (!NEEDS.includes(b.need_type)) e.need_type = 'Please choose what you need.';
  if (!URG.includes(b.urgency)) e.urgency = 'Please choose an urgency.';
  if (b.source !== undefined && b.source !== 'citizen') e.source = 'Invalid source.';
  const m = b.metadata || {};
  const pc = m.people_count === undefined ? 1 : Number(m.people_count);
  if (!Number.isInteger(pc) || pc < 1 || pc > 10000) e.people_count = 'Number of people must be at least 1.';
  const vulnerable = Array.isArray(m.vulnerable) ? [...new Set(m.vulnerable)] : [];
  if (vulnerable.some(v => !VULN.includes(v))) e.vulnerable = 'Invalid selection.';
  let phone = null;
  if (m.phone) {
    phone = clean(m.phone);
    if (!/^\+?[0-9 ()\-.]{7,20}$/.test(phone) || phone.replace(/\D/g, '').length < 7) e.phone = 'Please enter a valid phone number.';
  }
  const token = typeof b.client_token === 'string' && /^[\w-]{8,64}$/.test(b.client_token) ? b.client_token : null;
  const metadata = { people_count: pc, vulnerable, here: m.here !== false };
  if (phone) metadata.phone = phone;
  return { e, v: { text, location, lat, lng, need_type: b.need_type, urgency: b.urgency, metadata, token } };
}

app.post('/api/reports', async (req, res) => {
  const { e, v } = validate(req.body);
  if (Object.keys(e).length) return res.status(400).json({ ok: false, errors: e });
  try {
    const r = await pool.query(
      `INSERT INTO reports (text, location, lat, lng, need_type, urgency, source, metadata)
       VALUES ($1,$2,$3,$4,$5,$6,'citizen',$7)
       RETURNING id, text, location, lat, lng, need_type, urgency, source, "timestamp", metadata`,
      [v.text, v.location, v.lat, v.lng, v.need_type, v.urgency, v.metadata]
    );
    res.status(201).json({ ok: true, report: r.rows[0] });
  } catch (err) {
    console.error('[POST /api/reports]', err);
    res.status(500).json({ ok: false, message: "We couldn't submit your report right now. Please try again." });
  }
});

// Read endpoint for the coordinator dashboard (add auth before exposing publicly!)
app.get('/api/reports', async (_q, res) => {
  try {
    const r = await pool.query(`SELECT id, text, location, lat, lng, need_type, urgency, source, "timestamp", metadata
      FROM reports ORDER BY CASE urgency WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'moderate' THEN 2 ELSE 3 END, "timestamp" DESC LIMIT 500`);
    res.json(r.rows);
  } catch (err) { console.error(err); res.status(500).json({ ok: false, message: 'Could not load reports.' }); }
});

app.listen(process.env.PORT || 3000, () => console.log('Listening on http://localhost:' + (process.env.PORT || 3000) + '/report'));
