export type BoardOrientation = "white" | "black";

export type ResolvedReview = {
  analysisId: string;
  moveIndex: number;
  result: 'pending' | 'pass' | 'fail';
};

export type OpenHistoryOptions = {
  select: "latest";
  source: "post_game_view_analysis" | "post_game_history";
  sessionId?: string;
};
