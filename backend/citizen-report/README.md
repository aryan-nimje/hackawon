# Citizen incident reporting
1. `npm install`
2. Edit `.env` → `DATABASE_URL=<your PostgreSQL connection string>` (replace the placeholder)
3. `npm run migrate`   (creates the `reports` table from db/schema.sql)
4. `npm start` → http://localhost:3000/report
API: `POST /api/reports` (submit) · `GET /api/reports` (coordinator feed – add auth before exposing)
