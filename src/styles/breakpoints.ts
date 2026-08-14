/**
 * Canonical responsive breakpoints shared between TypeScript (useMediaQuery
 * callers) and the final game owner stylesheets.
 *
 * Media queries can't read JS values and CSS can't import TS, so these two
 * sides are kept in sync by convention + a guard test. When you change a value
 * here, update the matching `@media` block in
 * src/pages/GamePage.css and src/components/chess-game/ChessGame.css;
 * breakpoints.test.ts asserts that both owners still reference the same pixel
 * value so drift fails CI.
 */

/**
 * Width at/below which the /play game layout collapses from the two-column
 * mid-sized layout to the mobile single-column layout. Mirrors the
 * `@media (max-width: 659px)` blocks in GamePage.css and ChessGame.css.
 */
export const GAME_MOBILE_MAX_WIDTH = 659;

/** Media-query string form of {@link GAME_MOBILE_MAX_WIDTH}. */
export const GAME_MOBILE_QUERY = `(max-width: ${GAME_MOBILE_MAX_WIDTH}px)`;
