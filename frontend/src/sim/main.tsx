import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '../shared/theme.css';
import { loadLayers } from '../lib/layers';
import SimApp from './SimApp';

// Swap /layers data (hospitals, bridges, city) into the shared constants before the first render.
// Falls back to the built-in Pune defaults after 4 s or on any error.
void loadLayers().finally(() => {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <SimApp />
    </StrictMode>,
  );
});
