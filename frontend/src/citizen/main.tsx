import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '../index.css';
import { loadLayers } from '../lib/layers';
import CitizenApp from './CitizenApp';

// Swap /layers data (hospitals, bridges, city) into the shared constants before the first render.
// Falls back to the built-in Pune defaults after 4 s or on any error.
void loadLayers().finally(() => {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <CitizenApp />
    </StrictMode>,
  );
});
