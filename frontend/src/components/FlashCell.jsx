export default function FlashCell({ children, flash, className = '', align = 'right' }) {
  const flashClass = flash === 'up' ? 'flash-up' : flash === 'down' ? 'flash-down' : '';
  return (
    <span className={`flash-cell ${flashClass} ${className}`} style={{ textAlign: align }}>
      {children}
    </span>
  );
}
