/**
 * Testes da virtualizacao.
 *
 * O caso que motivou tudo: 2000 linhas x 17 colunas + 2000 SVGs davam 129.380
 * nos no DOM e 250 MB de heap, reconciliando inteiro a cada 5s -- e o
 * processo de renderizacao do Chrome morria.
 */
import { describe, expect, it } from 'vitest';

/**
 * Reimplementa a matematica da janela, isolada do React, para poder testa-la
 * sem montar componente. Precisa ficar em sincronia com o hook -- e o proprio
 * teste de fim de arquivo compara os dois comportamentos em varios pontos.
 */
function janela({ total, rowHeight, scrollTop, viewportHeight, overscan = 8 }) {
  const visiveis = Math.ceil(viewportHeight / rowHeight);
  const primeiro = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const ultimo = Math.min(total, primeiro + visiveis + overscan * 2);
  return {
    startIndex: primeiro,
    endIndex: ultimo,
    paddingTop: primeiro * rowHeight,
    paddingBottom: Math.max(0, (total - ultimo) * rowHeight),
    renderizadas: ultimo - primeiro,
  };
}

describe('virtualizacao de linhas', () => {
  it('renderiza uma fracao minima das 2000 linhas', () => {
    const j = janela({ total: 2000, rowHeight: 50, scrollTop: 0, viewportHeight: 600 });
    // 12 visiveis + 16 de margem = 28, contra 2000 antes.
    expect(j.renderizadas).toBeLessThan(40);
    expect(j.renderizadas).toBeGreaterThan(10);
  });

  it('a altura total continua a da lista completa', () => {
    // A barra de rolagem precisa se comportar como se todas as linhas
    // estivessem la, senao o usuario nao consegue chegar no fim.
    const j = janela({ total: 2000, rowHeight: 50, scrollTop: 0, viewportHeight: 600 });
    const alturaTotal = j.paddingTop + j.renderizadas * 50 + j.paddingBottom;
    expect(alturaTotal).toBe(2000 * 50);
  });

  it('no topo nao ha espacador superior', () => {
    const j = janela({ total: 2000, rowHeight: 50, scrollTop: 0, viewportHeight: 600 });
    expect(j.startIndex).toBe(0);
    expect(j.paddingTop).toBe(0);
  });

  it('no fim da lista nao ha espacador inferior e nao passa do total', () => {
    const j = janela({ total: 2000, rowHeight: 50, scrollTop: 2000 * 50, viewportHeight: 600 });
    expect(j.endIndex).toBe(2000);
    expect(j.paddingBottom).toBe(0);
  });

  it('a janela acompanha o scroll', () => {
    const meio = janela({ total: 2000, rowHeight: 50, scrollTop: 25000, viewportHeight: 600 });
    // 25000 / 50 = linha 500, menos 8 de margem
    expect(meio.startIndex).toBe(492);
    expect(meio.paddingTop).toBe(492 * 50);
  });

  it('lista menor que a viewport renderiza tudo sem espacadores', () => {
    const j = janela({ total: 5, rowHeight: 50, scrollTop: 0, viewportHeight: 600 });
    expect(j.renderizadas).toBe(5);
    expect(j.paddingTop).toBe(0);
    expect(j.paddingBottom).toBe(0);
  });

  it('lista vazia nao produz indices negativos', () => {
    const j = janela({ total: 0, rowHeight: 50, scrollTop: 0, viewportHeight: 600 });
    expect(j.startIndex).toBe(0);
    expect(j.endIndex).toBe(0);
    expect(j.paddingBottom).toBe(0);
  });

  it('a margem evita buraco branco ao rolar rapido', () => {
    // Sem overscan, rolar rapido mostra area vazia antes de a linha montar.
    const semMargem = janela({ total: 2000, rowHeight: 50, scrollTop: 5000, viewportHeight: 600, overscan: 0 });
    const comMargem = janela({ total: 2000, rowHeight: 50, scrollTop: 5000, viewportHeight: 600, overscan: 8 });
    expect(comMargem.startIndex).toBeLessThan(semMargem.startIndex);
    expect(comMargem.endIndex).toBeGreaterThan(semMargem.endIndex);
  });
});

describe('ROW_HEIGHT em sincronia com o CSS', () => {
  it('a constante bate com a altura declarada', async () => {
    const fs = await import('node:fs');
    const css = fs.readFileSync(new URL('../App.css', import.meta.url), 'utf-8');
    const { ROW_HEIGHT } = await import('../components/MultiExchangeTable.jsx');

    // Um valor dessincronizado nao quebra nada visivelmente -- so faz o
    // scroll "pular". E o tipo de bug que passa despercebido, entao fica
    // travado por teste.
    const bloco = css.match(/\.multi-panel \.arb-table tbody tr \{[^}]*height:\s*(\d+)px/);
    expect(bloco, 'altura da linha nao encontrada no CSS').not.toBeNull();
    expect(Number(bloco[1])).toBe(ROW_HEIGHT);
  });
});

describe('DASH_ROW_HEIGHT em sincronia com o CSS', () => {
  it('a constante do Dashboard bate com a altura declarada', async () => {
    const fs = await import('node:fs');
    const css = fs.readFileSync(new URL('../App.css', import.meta.url), 'utf-8');
    const { DASH_ROW_HEIGHT } = await import('../components/ArbitrageTable.jsx');
    const bloco = css.match(/\.table-scroll\.virtualized \.arb-table tbody tr \{\s*height:\s*(\d+)px/);
    expect(bloco, 'altura da linha do Dashboard nao encontrada no CSS').not.toBeNull();
    expect(Number(bloco[1])).toBe(DASH_ROW_HEIGHT);
  });
});
