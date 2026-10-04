require('dotenv').config();
const fs = require('fs'), { Pool } = require('pg');
(async () => {
  const pool = new Pool({ connectionString: process.env.DATABASE_URL });
  try { await pool.query(fs.readFileSync(__dirname + '/schema.sql', 'utf8')); console.log('reports table ready'); }
  catch (e) { console.error('Migration failed:', e.message); process.exitCode = 1; }
  finally { await pool.end(); }
})();
