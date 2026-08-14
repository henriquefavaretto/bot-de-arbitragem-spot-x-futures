export default function Header({ theme, onToggleTheme }) {
  return (
    <header className="app-header">
      <div className="brand">
        <span className="brand-mark">◆</span>
        <div className="brand-text">
          <h1>MEXC Arb Terminal</h1>
          <span className="brand-sub">Spot × Futures spread monitor</span>
        </div>
      </div>

      <button className="theme-toggle" onClick={onToggleTheme} aria-label="Alternar tema">
        {theme === 'dark' ? '☀️ Claro' : '🌙 Escuro'}
      </button>
    </header>
  );
}
