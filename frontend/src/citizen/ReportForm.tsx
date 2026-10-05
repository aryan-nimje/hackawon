import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { reportsApi, type SubmitResult } from '../api/reports';
import { CITY, haversineKm } from '../lib/layers';
import type { HelpType, ReportSubmission, VulnerableGroup } from '../types';
import { searchAddress, reverseGeocode, type GeoResult } from './geocode';
import { LocationPicker } from './LocationPicker';
import { useGeolocation } from './useGeolocation';

const NEEDS: { key: HelpType; label: string; icon: string }[] = [
  { key: 'rescue', label: 'Rescue', icon: '🛟' },
  { key: 'medical', label: 'Medical', icon: '🩺' },
  { key: 'shelter', label: 'Shelter', icon: '🏠' },
  { key: 'supplies', label: 'Food / water', icon: '🥫' },
  { key: 'other', label: 'Other', icon: '❔' },
];

const GROUPS: { key: VulnerableGroup; label: string; icon: string }[] = [
  { key: 'children', label: 'Children', icon: '🧒' },
  { key: 'elderly', label: 'Elderly', icon: '🧓' },
  { key: 'disabled', label: 'Disability / limited mobility', icon: '♿' },
  { key: 'pregnant', label: 'Pregnant', icon: '🤰' },
  { key: 'medical_needs', label: 'Medical needs (insulin, oxygen…)', icon: '💊' },
];

const MAX_TEXT = 500;
const MIN_TEXT = 10;
const COOLDOWN_MS = 30_000;

interface Props {
  onSubmitted: (r: SubmitResult) => void;
}

export function ReportForm({ onSubmitted }: Props) {
  const geo = useGeolocation(true);

  const [pin, setPin] = useState<[number, number] | null>(null);
  const [pinSource, setPinSource] = useState<'gps' | 'manual' | 'search' | null>(null);
  const [focusKey, setFocusKey] = useState(0);
  const [isOwnLocation, setIsOwnLocation] = useState(true);
  const [locationText, setLocationText] = useState('');

  const [query, setQuery] = useState('');
  const [results, setResults] = useState<GeoResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [need, setNeed] = useState<HelpType | null>(null);
  const [groups, setGroups] = useState<VulnerableGroup[]>([]);
  const [people, setPeople] = useState(1);
  const [text, setText] = useState('');
  const [contact, setContact] = useState('');
  const [website, setWebsite] = useState(''); // honeypot

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  const lastSubmit = useRef(0);
  const addressTouched = useRef(false);

  // First good GPS fix places the pin, unless the person already placed it themselves.
  useEffect(() => {
    if (geo.fix && pinSource !== 'manual' && pinSource !== 'search') {
      setPin([geo.fix.lat, geo.fix.lng]);
      setPinSource('gps');
      setFocusKey((k) => k + 1);
    }
  }, [geo.fix, pinSource]);

  // Best-effort street address for the pin (never blocks submission).
  useEffect(() => {
    if (!pin || addressTouched.current) return;
    const t = setTimeout(async () => {
      const label = await reverseGeocode(pin[0], pin[1]);
      if (label && !addressTouched.current) setLocationText(label);
    }, 700);
    return () => clearTimeout(t);
  }, [pin]);

  const accuracy = pinSource === 'gps' ? geo.fix?.accuracy ?? null : null;
  const poorAccuracy = pinSource === 'gps' && accuracy != null && accuracy > 150;
  const farFromCity = pin ? haversineKm(pin[0], pin[1], CITY.center[0], CITY.center[1]) > 80 : false;

  const errors = useMemo(() => {
    const e: string[] = [];
    if (!pin) e.push('Set the location where help is needed (use GPS, search an address, or tap the map).');
    if (!isOwnLocation && pinSource === 'gps') e.push('Move the pin to where the other person is.');
    if (text.trim().length < MIN_TEXT) e.push(`Describe what happened (at least ${MIN_TEXT} characters).`);
    return e;
  }, [pin, isOwnLocation, pinSource, text]);

  const movePin = (lat: number, lng: number) => {
    setPin([lat, lng]);
    setPinSource('manual');
    addressTouched.current = false;
  };

  const doSearch = async () => {
    setSearching(true);
    setSearchError(null);
    try {
      const r = await searchAddress(query, CITY.center);
      setResults(r);
      if (!r.length) setSearchError('No matches. Try a street name and city, or tap the map.');
    } catch (e) {
      setSearchError(e instanceof Error ? e.message : 'Address search failed');
    } finally {
      setSearching(false);
    }
  };

  const pickResult = (r: GeoResult) => {
    setPin([r.lat, r.lng]);
    setPinSource('search');
    setLocationText(r.label);
    addressTouched.current = true;
    setResults([]);
    setFocusKey((k) => k + 1);
  };

  const toggleGroup = (g: VulnerableGroup) =>
    setGroups((cur) => (cur.includes(g) ? cur.filter((x) => x !== g) : [...cur, g]));

  const onSubmit = async (ev: FormEvent) => {
    ev.preventDefault();
    setTouched(true);
    setSubmitError(null);
    if (errors.length || !pin) return;
    if (Date.now() - lastSubmit.current < COOLDOWN_MS) {
      setSubmitError('Please wait a moment before sending another report.');
      return;
    }
    const body: ReportSubmission = {
      text: text.trim().slice(0, MAX_TEXT),
      lat: pin[0],
      lng: pin[1],
      accuracy_m: accuracy,
      location_text: locationText.trim().slice(0, 200),
      need_type: need,
      vulnerable: groups,
      people_count: people,
      is_own_location: isOwnLocation,
      reporter_lat: geo.fix?.lat ?? null,
      reporter_lng: geo.fix?.lng ?? null,
      contact: contact.trim() || null,
      website,
    };
    setSubmitting(true);
    try {
      const res = await reportsApi.submit(body);
      lastSubmit.current = Date.now();
      onSubmitted(res);
    } catch (e) {
      setSubmitError(
        e instanceof Error && /429/.test(e.message)
          ? 'Too many reports from this device. Please wait a minute and try again.'
          : 'Could not send your report. Check your connection and try again.',
      );
    } finally {
      setSubmitting(false);
    }
  };

  const geoMessage = {
    idle: null,
    requesting: 'Finding your location…',
    granted: accuracy != null ? `Location found (accurate to about ${Math.round(accuracy)} m).` : 'Location found.',
    denied: 'Location permission was denied. Search an address or tap the map to place the pin.',
    unavailable: 'Your device could not provide a location. Search an address or tap the map.',
    timeout: 'Finding your location took too long. Try again, or tap the map.',
    insecure: 'Automatic location needs a secure (HTTPS) connection. Search an address or tap the map.',
  }[geo.state];

  return (
    <form onSubmit={onSubmit} className="space-y-5" noValidate>
      {/* 1 — location */}
      <Card step={1} title="Where is help needed?">
        <p
          className={`mb-2 text-sm ${
            geo.state === 'granted' && !poorAccuracy ? 'text-green-300' : geo.state === 'requesting' ? 'text-slate-300' : 'text-amber-300'
          }`}
          aria-live="polite"
        >
          {geoMessage}
          {poorAccuracy && ' The location is approximate — drag the pin to the exact spot.'}
        </p>

        <LocationPicker position={pin} accuracy={accuracy} focusKey={focusKey} onMove={movePin} />
        <p className="mt-2 text-xs text-slate-500">Drag the red pin or tap the map to adjust. The blue circle shows GPS accuracy.</p>

        <div className="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => {
              addressTouched.current = false;
              if (geo.fix) {
                setPin([geo.fix.lat, geo.fix.lng]);
                setFocusKey((k) => k + 1);
              }
              setPinSource('gps');
              geo.request();
            }}
            className="min-h-11 rounded-lg bg-slate-700 px-4 text-sm font-medium hover:bg-slate-600"
          >
            📍 Use my location
          </button>
        </div>

        <div className="mt-3">
          <label htmlFor="addr" className="text-sm text-slate-300">
            Or search an address
          </label>
          <div className="mt-1 flex gap-2">
            <input
              id="addr"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  doSearch();
                }
              }}
              placeholder="e.g. 1200 Main St"
              autoComplete="street-address"
              className="min-h-11 min-w-0 flex-1 rounded-lg border border-disaster-border bg-slate-900 px-3 text-base"
            />
            <button
              type="button"
              onClick={doSearch}
              disabled={searching || query.trim().length < 3}
              className="min-h-11 rounded-lg bg-slate-700 px-4 text-sm font-medium hover:bg-slate-600 disabled:opacity-50"
            >
              {searching ? '…' : 'Search'}
            </button>
          </div>
          {searchError && <p className="mt-1 text-xs text-amber-300">{searchError}</p>}
          {results.length > 0 && (
            <ul className="mt-2 overflow-hidden rounded-lg border border-disaster-border">
              {results.map((r) => (
                <li key={`${r.lat},${r.lng}`}>
                  <button
                    type="button"
                    onClick={() => pickResult(r)}
                    className="block w-full border-b border-disaster-border bg-slate-900 px-3 py-2 text-left text-sm last:border-b-0 hover:bg-slate-800"
                  >
                    {r.label}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="mt-3">
          <label htmlFor="landmark" className="text-sm text-slate-300">
            Address / landmark notes <span className="text-slate-500">(optional)</span>
          </label>
          <input
            id="landmark"
            value={locationText}
            onChange={(e) => {
              addressTouched.current = true;
              setLocationText(e.target.value);
            }}
            maxLength={200}
            placeholder="Building, floor, gate, nearby landmark"
            className="mt-1 min-h-11 w-full rounded-lg border border-disaster-border bg-slate-900 px-3 text-base"
          />
        </div>

        <fieldset className="mt-4">
          <legend className="text-sm font-medium text-slate-200">Is this where help is needed?</legend>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <Radio checked={isOwnLocation} onChange={() => setIsOwnLocation(true)} label="Yes — I'm here" />
            <Radio
              checked={!isOwnLocation}
              onChange={() => setIsOwnLocation(false)}
              label="No — I'm reporting for someone else"
            />
          </div>
          {!isOwnLocation && (
            <p className="mt-2 rounded-lg border border-sky-400/30 bg-sky-400/10 px-3 py-2 text-sm text-sky-200">
              Move the pin to where the other person is (search their address or drag the pin).
            </p>
          )}
        </fieldset>

        {farFromCity && (
          <p className="mt-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
            This pin is far from the affected area ({CITY.name}). Reports outside it may be flagged for manual review.
          </p>
        )}
      </Card>

      {/* 2 — need + people */}
      <Card step={2} title="What do you need?">
        <div className="flex flex-wrap gap-2" role="group" aria-label="Type of help">
          {NEEDS.map((n) => (
            <button
              key={n.key}
              type="button"
              aria-pressed={need === n.key}
              onClick={() => setNeed(need === n.key ? null : n.key)}
              className={`min-h-11 rounded-full border px-4 text-sm font-medium transition ${
                need === n.key
                  ? 'border-disaster-accent bg-disaster-accent text-slate-900'
                  : 'border-disaster-border bg-slate-900 text-slate-200 hover:bg-slate-800'
              }`}
            >
              <span aria-hidden="true">{n.icon}</span> {n.label}
            </button>
          ))}
        </div>

        <fieldset className="mt-5">
          <legend className="text-sm font-medium text-slate-200">Is anyone with you who needs extra care?</legend>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            {GROUPS.map((g) => (
              <label
                key={g.key}
                className={`flex min-h-11 cursor-pointer items-center gap-3 rounded-lg border px-3 py-2 text-sm ${
                  groups.includes(g.key)
                    ? 'border-disaster-accent bg-sky-400/10'
                    : 'border-disaster-border bg-slate-900 hover:bg-slate-800'
                }`}
              >
                <input
                  type="checkbox"
                  checked={groups.includes(g.key)}
                  onChange={() => toggleGroup(g.key)}
                  className="h-5 w-5 accent-sky-400"
                />
                <span aria-hidden="true">{g.icon}</span>
                {g.label}
              </label>
            ))}
          </div>
        </fieldset>

        <div className="mt-4 flex items-center gap-3">
          <span className="text-sm text-slate-200" id="people-label">
            How many people need help?
          </span>
          <div className="flex items-center gap-1" role="group" aria-labelledby="people-label">
            <button
              type="button"
              onClick={() => setPeople((p) => Math.max(1, p - 1))}
              className="h-11 w-11 rounded-lg bg-slate-700 text-lg hover:bg-slate-600"
              aria-label="Fewer people"
            >
              −
            </button>
            <span className="w-10 text-center text-lg font-semibold" aria-live="polite">
              {people}
            </span>
            <button
              type="button"
              onClick={() => setPeople((p) => Math.min(99, p + 1))}
              className="h-11 w-11 rounded-lg bg-slate-700 text-lg hover:bg-slate-600"
              aria-label="More people"
            >
              +
            </button>
          </div>
        </div>
      </Card>

      {/* 3 — what happened */}
      <Card step={3} title="What happened?">
        <label htmlFor="what" className="sr-only">
          Describe the situation
        </label>
        <textarea
          id="what"
          value={text}
          onChange={(e) => setText(e.target.value.slice(0, MAX_TEXT))}
          rows={4}
          placeholder="e.g. Water is up to the 2nd step, my father uses a wheelchair and can't leave."
          className="w-full rounded-lg border border-disaster-border bg-slate-900 px-3 py-2 text-base"
          aria-describedby="what-count"
        />
        <p id="what-count" className="mt-1 text-right text-xs text-slate-500">
          {text.length}/{MAX_TEXT}
        </p>

        <label htmlFor="contact" className="mt-3 block text-sm text-slate-300">
          Phone number <span className="text-slate-500">(optional — visible to coordinators only)</span>
        </label>
        <input
          id="contact"
          value={contact}
          onChange={(e) => setContact(e.target.value.slice(0, 40))}
          inputMode="tel"
          autoComplete="tel"
          className="mt-1 min-h-11 w-full rounded-lg border border-disaster-border bg-slate-900 px-3 text-base"
        />

        {/* honeypot: real people never see or fill this */}
        <div aria-hidden="true" className="absolute -left-[9999px] h-0 w-0 overflow-hidden">
          <label>
            Website
            <input tabIndex={-1} autoComplete="off" value={website} onChange={(e) => setWebsite(e.target.value)} />
          </label>
        </div>
      </Card>

      {touched && errors.length > 0 && (
        <ul className="space-y-1 rounded-xl border border-red-700/60 bg-red-950/30 px-4 py-3 text-sm text-red-200" role="alert">
          {errors.map((e) => (
            <li key={e}>• {e}</li>
          ))}
        </ul>
      )}
      {submitError && (
        <p className="rounded-xl border border-red-700/60 bg-red-950/30 px-4 py-3 text-sm text-red-200" role="alert">
          {submitError}
        </p>
      )}

      <button
        type="submit"
        disabled={submitting}
        className="min-h-14 w-full rounded-xl bg-disaster-danger text-lg font-bold text-white shadow-lg hover:bg-red-400 disabled:opacity-60"
      >
        {submitting ? 'Sending…' : 'Send emergency report'}
      </button>
      <p className="text-center text-xs text-slate-500">
        Your exact location is shared only with response coordinators and is never shown publicly.
      </p>
    </form>
  );
}

function Card({ step, title, children }: { step: number; title: string; children: React.ReactNode }) {
  return (
    <section className="relative rounded-2xl border border-disaster-border bg-disaster-panel p-4 sm:p-5">
      <h2 className="mb-3 flex items-center gap-2 text-base font-semibold">
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-slate-700 text-xs">{step}</span>
        {title}
      </h2>
      {children}
    </section>
  );
}

function Radio({ checked, onChange, label }: { checked: boolean; onChange: () => void; label: string }) {
  return (
    <label
      className={`flex min-h-11 cursor-pointer items-center gap-3 rounded-lg border px-3 py-2 text-sm ${
        checked ? 'border-disaster-accent bg-sky-400/10' : 'border-disaster-border bg-slate-900 hover:bg-slate-800'
      }`}
    >
      <input type="radio" checked={checked} onChange={onChange} className="h-5 w-5 accent-sky-400" />
      {label}
    </label>
  );
}
