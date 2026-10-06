# Disaster Relief frontend: two apps here (+ a separately hosted citizen app)

| URL | App | Role |
|---|---|---|
| `/` | Authority command | Read-only map, live feed, **the only place for decisions** (plan review, re-route approval) |
| `/sim` | Simulation | Read-write map: starts/pauses/resets the scenario, injects faults |

**Citizen report form: not in this repo.** It is a separate, explicitly hosted version (different codebase/deploy). It was removed from here on purpose; the task is still covered, just not by this code. It writes reports straight into the shared PostgreSQL `reports` table; this backend only reads that table (see `backend/services/citizen_db.py`, `citizen_sync.py`) and serves it to the dashboard via `GET /reports` and the `report.new` event. No CORS or API link between the hosted app and this backend is needed.

The apps share **no browser state**. Everything crosses over the network through the backend, so each can run on a different device.
Authority and Simulation render the same `src/shared/map/WorldMap.tsx` (`mode="readonly"` vs `mode="edit"`).

## Run

```bash
npm install
cp .env.example .env     # set VITE_API_BASE_URL
npm run dev              # use this, not Live Server (multi-page routing + proxy)
```

Different devices: run the dev server on one machine (`host: 0.0.0.0`), open `http://<LAN-IP>:5173/`, `/sim`, `/report`
from any device, and set `VITE_API_BASE_URL=http://<BACKEND-LAN-IP>:8742` so every device reaches the same backend.

## Backend contract the frontend assumes (base `VITE_API_BASE_URL`)

- SSE `GET /bus/stream`: replays current state on connect, then pushes
  `world` (WorldState), `sim_event` (SimBusEvent), `plan.updated` / `plan.approved` (PlanPayload),
  `plan.replan` ({item_id, action}), `report.new` (CitizenReport), `execution.update` (ExecutionItem[])
- `POST /sim/world` sim publishes WorldState (about 2/s) · `POST /sim/event` sim publishes discrete events
- `POST /scenario/start` (Sim only) returns `{run_id}`
- `POST /plan/:runId/review` `{actions:[{item_id, action, edited_content?}]}` (Authority only)
- `POST /plan/:runId/replan` `{item_id, action:'reroute'|'hold'}` (Authority only; relayed as `plan.replan`)
- `GET /reports` returns CitizenReport[]

All payload types are in `src/shared/types.ts`.

## Pune data, active run, beds, tracked positions (added)

**City layers.** `src/lib/layers.ts` ships built-in Pune defaults (hospitals, depots, flood zones, bridges, centre).
Before each app renders, `loadLayers()` calls `GET /layers?city=<name>` (city from `?city=` in the URL, else `VITE_CITY`, else `Pune`)
and swaps the result into the shared constants. 4 s timeout; on any failure or bad payload the Pune defaults stay.
Expected shape: `{city:{name,center:[lat,lng],zoom}, hospitals:[{id,name,lat,lng,beds,specialties}], depots:[{id,name,lat,lng,stock}],
flood_zones:[{id,name,reason,ring:[[lat,lng],...]}], bridges:[{id,name,geometry:[[lat,lng],[lat,lng]]}]}`.
Hospital ids must match the ids the backend agents use. Scenario presets are built from the city centre, so they follow the loaded city.
A place search box (Nominatim, on submit only) is on every `WorldMap`.

**Active run.** `WorldState.run_id` is published by the sim. Authority follows `world.run_id` (else the newest plan payload) and shows only
that run's plan; the simulator pins its own run. Reports with a different `run_id` are hidden; reports without one are always shown.
Starting or resetting a sim clears browser-only demo reports (`reportsDemo`).

**Hospital beds (sim).** `src/sim/hospitals.ts` is a patient ledger: every patient (walk-in, incident, or held by the operator) has a
length of stay and keeps a bed until it ends; `dischargeDue` then frees it. Occupancy is the number of patients in the ledger, so
admissions, discharges, operator changes and the published census cannot drift apart (free beds = capacity - occupied, always).
- **Walk-in demand** is a Poisson process per hospital. A normal day holds ~50% occupancy; demand is multiplied by a factor that grows with
  the severity and size of every hazard region and decays with the distance of the hospital from it (`demandMultiplier`). Disasters also shift
  the mix towards major injuries, which stay longer. Stays: minor 15-35, moderate 35-70, major 70-120 sim-minutes.
- **Incident patients** take a bed on arrival for the Medical Agent's `expected_stay_min`.
- **Full or offline** hospitals divert new patients (walk-ins from their catchment included) to the nearest hospital with room; if none has
  room the patient overflows (counted in the score card). Diversions of walk-ins are announced in batches, not one by one.
- Operator tools (`-5/+5 free`, mark full, take offline, undo) act on the ledger; a held bed is released when its stay ends.
- The world carries `hospital_status`, `hospital_load` (per-hospital census) and `admitted_incident_ids`; the backend relays them and the
  Medical Agent plans against them. The Sim panel, the map (marker colour and popup) and Authority's "Hospital capacity" panel all show occupancy.

`scripts/check-hospitals.ts` checks the model headlessly (demand scaling, stays, discharge, diversion/overflow, recovery, undo); run
`npx esbuild scripts/check-hospitals.ts --bundle --platform=node --format=esm --outfile=/tmp/check.mjs --define:import.meta.env='{}' && node /tmp/check.mjs`.

**Tracked positions.** `WorldVehicle` / `ExecutionItem` accept `position_source: 'estimated'|'reported'` and `last_update_ts` (epoch ms).
Maps and the tracker label estimated positions and flag a vehicle stale after 30 s without an update (`STALE_AFTER_MS`).
The sim publishes `reported`; backend dead-reckoned positions should publish `estimated`.

Not changed: the `/sim/*` endpoint and `sim_*` field names (kept to match the agreed backend contract).

## Switching city by moving the map

In **Sim**, when the map settles (about 1 s after you stop moving) outside the loaded city and at zoom 9 or closer, the app
asks Nominatim which city it is, loads `GET /layers?city=<name>` (the backend fetches and caches it on first use, which can
take a minute) and swaps the layers in place (`loadCity`, `subscribeLayers`, `useCityVersion`). Starting the simulation
sends `city: <slug>`, so the backend plans for that city, and the city is locked until Reset. **Authority** is read-only: it
loads whatever city the active run declares (`run.active.city`). A failed load keeps the current city and shows the reason.

## Simulation: incident evidence, reset

- **Incident evidence** is in the incident's map popup in `/sim` (click an incident marker; `IncidentEvidenceActions` in
  `src/sim/Panels.tsx`): the current credibility (or *pending verification*), **Backs this incident** (News evidence, Official
  alert, Meteorological evidence), **Contradicts this incident** (All clear / false alarm, Normal relevant conditions) and
  **Randomise** for that incident. Each button calls `POST /sim/evidence`; the backend builds the evidence and scores it with the
  normal pipeline. Credibility updates from the live stream.
- **Authority "Incident stream"** lists live news (GDELT) and simulated evidence (labelled backs / contradicts); the scripted
  "[SIMULATED NEWS]" items only show while a simulation is running. **External alerts** lists all active signals.
- **Reset simulation** is always available and calls `POST /sim/reset` first; the local world is only cleared once the backend
  succeeded, and nothing is published afterwards. Runs are started with `simulation: true` so the backend can tell them from real ones.
- The simulator's blocking rule mirrors the Route Agent: a team stopped at a blockage may only leave it.
