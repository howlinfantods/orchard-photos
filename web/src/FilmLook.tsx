import { useEffect, useRef, useState } from 'react';
import { ChevronDown, Film } from 'lucide-react';
import { post, request, storageKey } from './types';

/** A film look from Fotufilm: a stock, a film format, and the engine controls Orchard offers. */
export interface Look { stock: string; format: string; exposure: number; grain: number; push: number }
interface Stock { id: string; name: string; format: string | null }
export interface FilmExport { state: 'idle' | 'running' | 'done' | 'cancelled'; done?: number; failed?: number; total?: number; folder?: string }
export interface FilmStatus { available: boolean; reason?: string; backend?: string; stocks?: Stock[]; formats?: string[]; export: FilmExport }

export const NO_LOOK: Look = { stock: '', format: '', exposure: 0, grain: 1, push: 0 };
const STORAGE = storageKey('look');
const FORMAT_NAMES: Record<string, string> = { '35mm': '35 mm', '120': '120 medium format', '4x5': '4×5 large format', super35: 'Super 35', '16mm': '16 mm', super8: 'Super 8', instaxmini: 'Instax Mini', instaxsquare: 'Instax Square', instaxwide: 'Instax Wide' };
// How long a slider rests before the look is developed again.
const SETTLE_MS = 350;

export function lookParams(look: Look) {
  return { stock: look.stock, ...(look.format && { format: look.format }), exposure: String(look.exposure), grain: String(look.grain), push: String(look.push) };
}
/** The photo developed through a look at about `edge` pixels; the server rounds the size so window sizes share a picture. */
export const filmUrl = (id: string, look: Look, edge: number) => `/api/images/${id}/film?${new URLSearchParams({ ...lookParams(look), edge: String(Math.round(edge)) })}`;

export function readLook(): Look {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE) || '{}');
    const number = (value: unknown, fallback: number) => typeof value === 'number' && Number.isFinite(value) ? value : fallback;
    return { stock: typeof saved.stock === 'string' ? saved.stock : '', format: typeof saved.format === 'string' ? saved.format : '', exposure: number(saved.exposure, 0), grain: number(saved.grain, 1), push: number(saved.push, 0) };
  } catch { return NO_LOOK; }
}
function saveLook(look: Look) {
  try { localStorage.setItem(STORAGE, JSON.stringify(look)); } catch { /* The look is kept for this visit only. */ }
}

/** Fotufilm's status, fetched once per page. */
let statusRequest: Promise<FilmStatus> | null = null;
export function useFilm() {
  const [status, setStatus] = useState<FilmStatus | null>(null);
  useEffect(() => {
    statusRequest ??= request<FilmStatus>('/api/film').catch(() => ({ available: false, reason: 'Fotufilm status is unavailable.', export: { state: 'idle' } } as FilmStatus));
    let live = true;
    void statusRequest.then(result => { if (live) setStatus(result); });
    return () => { live = false; };
  }, []);
  return status;
}

/** The look chosen in the viewer, kept between photos and visits. Sliders settle before the photo is developed again. */
export function useLook() {
  const [draft, setDraft] = useState<Look>(readLook);
  const [look, setLook] = useState<Look>(draft);
  useEffect(() => {
    const timer = setTimeout(() => { setLook(draft); saveLook(draft); }, draft.stock === look.stock && draft.format === look.format ? SETTLE_MS : 0);
    return () => clearTimeout(timer);
  }, [draft, look.stock, look.format]);
  return { draft, look, setDraft };
}

export function FilmPanel({ status, draft, onChange, ids, onMessage }: { status: FilmStatus | null; draft: Look; onChange: (look: Look) => void; ids: string[]; onMessage: (message: string) => void }) {
  const [exporting, setExporting] = useState<FilmExport | null>(null);
  const polling = useRef(0);
  useEffect(() => () => clearTimeout(polling.current), []);
  if (!status) return null;
  if (!status.available) return <p className="film-note">{status.reason}</p>;
  const stocks = status.stocks ?? [];
  const exportLook = async () => {
    onMessage('');
    try {
      let state = await post<FilmExport>('/api/film/export', { ids, look: lookParams(draft) });
      setExporting(state);
      while (state.state === 'running') {
        await new Promise(resolve => { polling.current = window.setTimeout(resolve, 700); });
        state = (await request<FilmStatus>('/api/film')).export;
        setExporting(state);
      }
      onMessage(state.state === 'done' ? `Saved ${state.done} to ${state.folder}${state.failed ? `; ${state.failed} could not be developed` : ''}` : 'Export cancelled');
    } catch (e) { onMessage((e as Error).message); }
    setExporting(null);
  };
  const slider = (key: 'exposure' | 'grain' | 'push', label: string, min: number, max: number, step: number, unit: string) => <label className="film-slider">
    <span>{label}<output>{draft[key] > 0 && unit === 'ev' ? '+' : ''}{draft[key].toLocaleString(undefined, { maximumFractionDigits: 2 })}{unit === 'ev' ? ' EV' : unit}</output></span>
    <input type="range" min={min} max={max} step={step} value={draft[key]} onChange={e => onChange({ ...draft, [key]: Number(e.target.value) })} onDoubleClick={() => onChange({ ...draft, [key]: NO_LOOK[key] })} />
  </label>;
  return <section className="film-look" aria-label="Film look">
    <h2><Film />Film look</h2>
    <div className="select"><select aria-label="Film stock" value={draft.stock} onChange={e => onChange({ ...draft, stock: e.target.value })}>
      <option value="">Original</option>
      {stocks.map(stock => <option key={stock.id} value={stock.id}>{stock.name}</option>)}
    </select><ChevronDown /></div>
    {draft.stock && <>
      <div className="select"><select aria-label="Film format" value={draft.format} onChange={e => onChange({ ...draft, format: e.target.value })}>
        <option value="">Stock's own format</option>
        {(status.formats ?? []).map(format => <option key={format} value={format}>{FORMAT_NAMES[format] ?? format}</option>)}
      </select><ChevronDown /></div>
      {slider('exposure', 'Exposure', -2, 2, 0.25, 'ev')}
      {slider('push', 'Push / pull', -2, 2, 1, ' stops')}
      {slider('grain', 'Grain', 0, 2, 0.05, '×')}
      <button className="text-button" disabled={!!exporting} onClick={exportLook}>{exporting ? `Developing ${exporting.done ?? 0} of ${exporting.total ?? ids.length}…` : ids.length > 1 ? `Export ${ids.length.toLocaleString()} with this look` : 'Export with this look'}</button>
    </>}
    <p className="film-note">Developed by <a href="https://fotufilm.com" target="_blank" rel="noreferrer">Fotufilm</a>{status.backend ? ` on ${status.backend}` : ''}.</p>
  </section>;
}
