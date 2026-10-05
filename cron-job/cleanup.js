const { Client } = require('pg');
const client = new Client({ connectionString: process.env.DATABASE_URL });

async function purge() {
  await client.connect();
  const query = `DELETE FROM your_table_name WHERE created_at < NOW() - INTERVAL '24 hours';`;
  const res = await client.query(query);
  console.log(`Deleted ${res.rowCount} expired entries.`);
  await client.end();
}

purge().catch((err) => {
  console.error(err);
  process.exit(1);
});