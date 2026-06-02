import type { OpeningPlayerColor } from "../utils/api";

export type OpeningRoute = {
  playerColor: OpeningPlayerColor;
  openingKey?: string;
  path?: string[];
};

export function buildOpeningsSearchParams(route: OpeningRoute): URLSearchParams {
  const params = new URLSearchParams({
    color: route.playerColor,
  });

  if (route.openingKey) {
    params.set("opening", route.openingKey);

    for (const pathKey of route.path ?? []) {
      params.append("path", pathKey);
    }
  }

  return params;
}
