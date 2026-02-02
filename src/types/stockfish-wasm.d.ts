declare module 'stockfish.wasm' {
  export type StockfishMessageListener = (line: string) => void

  export type StockfishInstance = {
    postMessage(command: string): void
    addMessageListener(listener: StockfishMessageListener): void
    removeMessageListener(listener: StockfishMessageListener): void
    terminate(): void
  }

  export type StockfishInitOptions = {
    locateFile?: (path: string) => string
    mainScriptUrlOrBlob?: string | Blob
  }

  export default function Stockfish(
    options?: StockfishInitOptions,
  ): Promise<StockfishInstance>
}
