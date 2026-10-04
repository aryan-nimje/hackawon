require('dotenv').config();
const { Pool } = require('pg');
const p = new Pool({ connectionString: process.env.DATABASE_URL });
(async () => {
  const { rows } = await p.query(
    "SELECT data_type FROM information_schema.columns WHERE table_name='reports' AND column_name='id'");
  const t = rows[0] && rows[0].data_type;
  console.log('id column type:', t);
  if (t === 'uuid') {
    await p.query('ALTER TABLE reports ALTER COLUMN id SET DEFAULT gen_random_uuid()');
  } else if (t === 'text' || t === 'character varying') {
    await p.query('ALTER TABLE reports ALTER COLUMN id SET DEFAULT gen_random_uuid()::text');
  } else if (['integer', 'bigint', 'smallint'].includes(t)) {
    await p.query('CREATE SEQUENCE IF NOT EXISTS reports_id_seq OWNED BY reports.id');
    await p.query("SELECT setval('reports_id_seq', COALESCE((SELECT MAX(id) FROM reports),0)+1, false)");
    await p.query("ALTER TABLE reports ALTER COLUMN id SET DEFAULT nextval('reports_id_seq')");
  } else {
    console.log('Unexpected type, paste this output to me.');
    return;
  }
  console.log('done');
})().catch(e => console.error(e.message)).finally(() => p.end());