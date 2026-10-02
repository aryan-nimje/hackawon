/// <reference types="vite/client" />

declare module '*.geojson' {
  const value: {
    type: string;
    features: Array<{
      type: string;
      properties: { name?: string; reason?: string; [key: string]: string | undefined };
      geometry: { type: string; coordinates: number[][][] };
    }>;
  };
  export default value;
}
