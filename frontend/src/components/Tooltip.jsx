import { useState } from 'react';

export default function Tooltip({ content, children }) {
  const [visible, setVisible] = useState(false);

  if (!content) return children;

  return (
    <span
      className="tooltip-wrap"
      onMouseEnter={() => setVisible(true)}
      onMouseLeave={() => setVisible(false)}
    >
      {children}
      {visible && <span className="tooltip-bubble">{content}</span>}
    </span>
  );
}
