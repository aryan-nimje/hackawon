# Citizen incident reporting
1. `npm install`
2. Edit `.env` → `DATABASE_URL=<your PostgreSQL connection string>` (replace the placeholder)
3. `npm run migrate`   (creates the `reports` table from db/schema.sql)
4. `npm start` → http://localhost:3000/report
API: `POST /api/reports` (submit) · `GET /api/reports` (coordinator feed – add auth before exposing)

## Photo → description (OpenAI Vision)
Citizens can attach a photo; the server sends it to OpenAI and the result fills the "What happened?" textarea (editable). The image is never stored; `POST /api/reports` is unchanged.
- Add to `.env` (server-side only): `OPENAI_API_KEY=...` and optionally `OPENAI_VISION_MODEL=...` (default `gpt-4.1-mini`; non-reasoning models work best here).
- `npm install` (adds the `openai` package), then `npm run migrate` (safe to re-run; updates the `need_type` constraint to `rescue | medical | supplies`).
- Old rows with `shelter` / `food_water` / `other` are left as they are. To remap them, run e.g. `UPDATE reports SET need_type='supplies' WHERE need_type IN ('shelter','food_water'); ` and decide separately what to do with `other`.
