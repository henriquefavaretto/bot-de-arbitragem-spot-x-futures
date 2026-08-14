import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Virtualizacao de linhas: mantem no DOM so o que esta visivel.
 *
 * ## Por que existe
 *
 * Quando o dashboard multi-exchange passou a mostrar TODAS as moedas, a
 * tabela passou a renderizar ate 2000 linhas de uma vez. Medicao real no
 * navegador (05/08/2026):
 *
 *     129.380 nos no DOM      (2.588 linhas x 17 colunas + 2.578 SVGs)
 *     250 MB de heap
 *     100.035 px de altura renderizada de uma vez
 *
 * E isso reconciliava INTEIRO a cada snapshot do WebSocket, a cada ~5s. O
 * resultado foi o processo de renderizacao do Chrome morrendo ("Ah, nao!").
 *
 * Com virtualizacao, so as ~30 linhas visiveis (mais uma margem) existem no
 * DOM. As outras viram altura vazia em dois espacadores. A lista continua
 * completa e rolavel -- o que muda e quantas linhas o navegador precisa
 * manter vivas ao mesmo tempo.
 *
 * ## Por que altura fixa
 *
 * Todas as linhas medem exatamente 50px (verificado no navegador). Com
 * altura constante, a posicao de qualquer linha e uma multiplicacao, sem
 * precisar medir nada nem manter cache de alturas -- o que torna esta
 * implementacao pequena o suficiente para nao valer uma dependencia externa.
 *
 * Se o layout da linha mudar, `rowHeight` precisa mudar junto: um valor
 * dessincronizado nao quebra visivelmente, so faz o scroll "pular" -- o tipo
 * de bug que passa despercebido. Por isso ha um teste que compara a
 * constante com a altura declarada no CSS.
 */
export function useVirtualRows({ total, rowHeight, overscan = 8 }) {
  const containerRef = useRef(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(600);

  const onScroll = useCallback((e) => {
    setScrollTop(e.currentTarget.scrollTop);
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const medir = () => setViewportHeight(el.clientHeight || 600);
    medir();
    // ResizeObserver e o que mantem a janela correta quando o usuario
    // redimensiona a tela ou abre/fecha os filtros; sem ele, a lista
    // renderiza poucas linhas demais (buraco no fim) ou demais (desperdicio).
    const ro = new ResizeObserver(medir);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const visiveis = Math.ceil(viewportHeight / rowHeight);
  const primeiro = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const ultimo = Math.min(total, primeiro + visiveis + overscan * 2);

  return {
    containerRef,
    onScroll,
    startIndex: primeiro,
    endIndex: ultimo,
    // Espacadores: altura pura, sem conteudo. Sao eles que fazem a barra de
    // rolagem se comportar como se todas as linhas estivessem la.
    paddingTop: primeiro * rowHeight,
    paddingBottom: Math.max(0, (total - ultimo) * rowHeight),
  };
}
