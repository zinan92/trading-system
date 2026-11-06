/* The news rail and 「今天」 are two islands sharing one thing: which news Park cites in today's
 * judgment. Both are in this bundle, so a module store is enough. A different asset starts empty. */
import {useSyncExternalStore} from "react";
import type {Cite} from "./today";

interface State {asset: string | null; cites: Cite[]}
let state: State = {asset: null, cites: []};
const listeners = new Set<() => void>();
const emit = () => listeners.forEach(fn => fn());

export const citeStore = {
  get: () => state,
  subscribe(fn: () => void) { listeners.add(fn); return () => { listeners.delete(fn); }; },
  forAsset(asset: string) { if (state.asset !== asset) { state = {asset, cites: []}; emit(); } },
  set(asset: string, cites: Cite[]) { state = {asset, cites: [...cites]}; emit(); },
  toggle(asset: string, item: Cite) {
    if (state.asset !== asset) return;
    const has = state.cites.some(c => c.title === item.title);
    state = {asset, cites: has ? state.cites.filter(c => c.title !== item.title) : [...state.cites, {id: item.id, title: item.title, bucket: item.bucket}]};
    emit();
  },
  reset() { state = {asset: null, cites: []}; emit(); },
};

export function useCites(asset: string): Cite[] {
  const s = useSyncExternalStore(citeStore.subscribe, citeStore.get, citeStore.get);
  return s.asset === asset ? s.cites : [];
}
