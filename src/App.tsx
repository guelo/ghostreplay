import ChessGame from './components/ChessGame'
import './App.css'

const featureHighlights = [
  {
    title: 'Ghost opponent',
    description:
      'Relive critical mistakes on demand. The engine forces the same positions so habits get rebuilt under pressure.',
  },
  {
    title: 'Spaced repetition',
    description:
      'Mistakes cool down only after you prove you can avoid them. The tougher the blunder, the sooner it returns.',
  },
  {
    title: 'Live feedback',
    description:
      'Stockfish watches every move so you know immediately whether the leak is fixed or needs more reps.',
  },
]

function App() {
  return (
    <main className="app-shell">
      <section className="hero">
        <p className="eyebrow">Ghost Replay</p>
        <h1>Face the blunder. Fix the player.</h1>
        <p>
          This starter client boots the Ghost Replay experience you see in the
          product spec. React + Vite power the UI so follow-on features can ship
          quickly without wrestling the scaffolding.
        </p>
        <div className="cta-row">
          <button className="cta" type="button">
            Start a replay
          </button>
          <button className="cta secondary" type="button">
            View milestone plan
          </button>
        </div>
      </section>

      <section className="feature-grid">
        {featureHighlights.map((feature) => (
          <article key={feature.title} className="feature-card">
            <h2>{feature.title}</h2>
            <p>{feature.description}</p>
          </article>
        ))}
      </section>

      <ChessGame />

      <section className="status-card" aria-live="polite">
        <span>Status</span>
        <strong>Frontend scaffolding is ready for the next task.</strong>
        <p>
          Install dependencies <code>npm install</code> and run{' '}
          <code>npm run dev</code>.
        </p>
      </section>
    </main>
  )
}

export default App
