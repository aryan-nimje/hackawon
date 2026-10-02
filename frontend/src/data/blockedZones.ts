export const blockedZones = {
  type: 'FeatureCollection',
  features: [
    {
      type: 'Feature',
      properties: { name: 'Buffalo Bayou Flood Zone (SIMULATED)', reason: 'Overtopping / deep water' },
      geometry: {
        type: 'Polygon',
        coordinates: [[
          [-95.3750, 29.7580],
          [-95.3650, 29.7580],
          [-95.3650, 29.7680],
          [-95.3750, 29.7680],
          [-95.3750, 29.7580],
        ]],
      },
    },
    {
      type: 'Feature',
      properties: { name: 'I-45 North Underpass (SIMULATED)', reason: 'Highway flooded' },
      geometry: {
        type: 'Polygon',
        coordinates: [[
          [-95.3620, 29.8020],
          [-95.3580, 29.8020],
          [-95.3580, 29.8080],
          [-95.3620, 29.8080],
          [-95.3620, 29.8020],
        ]],
      },
    },
    {
      type: 'Feature',
      properties: { name: 'East Downtown Low-Lying Block (SIMULATED)', reason: 'Street flooding 3ft+' },
      geometry: {
        type: 'Polygon',
        coordinates: [[
          [-95.3480, 29.7480],
          [-95.3420, 29.7480],
          [-95.3420, 29.7540],
          [-95.3480, 29.7540],
          [-95.3480, 29.7480],
        ]],
      },
    },
  ],
} as const;
