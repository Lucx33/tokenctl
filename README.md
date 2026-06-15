<p align="center">
  <img src="assets/tokenctl-loop.gif" alt="tokenctl" width="640">
</p>

# tokenctl - AI Usage Viewer

Dashboard web do seu uso de assistentes de IA - hoje **Claude Code** e **Codex CLI**:
tokens (input/output/cache), mensagens/sessões e o **valor equivalente em API**
(quanto custaria se o uso fosse pago por token), quebrado por **provedor, dia,
modelo, projeto e máquina**.

Os dados vêm dos transcripts locais de cada ferramenta:
- Claude Code: `~/.claude/projects/**/*.jsonl`
- Codex CLI: `~/.codex/sessions/**/rollout-*.jsonl` (eventos `token_count`)

Como os dois rodam em plano de assinatura, o "valor" é o equivalente em API - útil
pra ver o quanto o plano está rendendo. O dashboard se **atualiza sozinho**: o
backend reparseia os dados a cada request e o front faz *polling* automático
(indicador "● live" no header), então novos usos aparecem sem reiniciar nada.

## Segurança e privacidade

**100% local, sem telemetria.** O `tokenctl` só lê arquivos no seu computador e
sobe um servidor em `127.0.0.1` (localhost) - não envia nada para lugar nenhum,
não tem analytics e não faz nenhuma chamada de rede de saída.

**Só lê informações de uso.** De cada transcript ele extrai apenas os metadados de
consumo: provedor, modelo, data/hora, contagem de tokens (input/output/cache) e o
nome da pasta do projeto. Para agrupar os números, guarda também o *hostname* da
máquina e o id (aleatório) da sessão.

**O que ele NÃO lê:** o conteúdo das suas conversas (prompts/respostas), chaves de
API, tokens de acesso, e-mail, id de conta/usuário ou qualquer credencial. Esses
campos simplesmente não são abertos nem exportados.

O export para juntar várias máquinas (`usage-<host>.jsonl`) carrega exatamente
esses mesmos metadados de uso - **nunca** o texto das conversas.

## Uso local (uma máquina)

```bash
cd ~/tokenctl
uv run app.py serve        # abre em http://127.0.0.1:8888
```

## Unificar várias máquinas (manual)

Cada máquina exporta só um resumo **enxuto** (`usage-<host>.jsonl`: modelo, tokens,
data, projeto, máquina - **sem o conteúdo das conversas**). Você junta esses
arquivos no diretório `data/` da máquina que roda o dashboard; o viewer mescla
tudo e deduplica por `message.id+requestId`, então nunca conta a mesma mensagem
duas vezes.

### 1. Em cada máquina secundária

```bash
uv run app.py export        # gera data/usage-<host>.jsonl desta máquina
```

### 2. Levar o arquivo para a máquina do dashboard

A forma mais fácil: no painel, clique em **`[ + multi-máquina ]`** (canto superior
direito) e arraste os `usage-*.jsonl` - pode soltar vários de uma vez. Eles são
gravados em `data/` e o painel já mostra os dados.

Ou copie manualmente para o `data/` da máquina que serve o painel - por `scp`,
Syncthing, pen drive, ou o que preferir. Por exemplo:

```bash
scp data/usage-*.jsonl usuario@maquina-do-dashboard:~/tokenctl/data/
```

Não há repositório git de dados nem push automático: é só deixar os `.jsonl` em
`data/`. Se quiser automatizar, aponte uma pasta sincronizada (ex.: Syncthing)
como `DATA_DIR`.

### 3. Ver o dashboard

```bash
uv run app.py serve    # sobe o painel já unificado com tudo que está em data/
```

A aba **"Por máquina"** aparece sozinha quando há dados de mais de um computador.

## Variáveis de ambiente

| Var                   | Padrão                  | O que faz                              |
|-----------------------|-------------------------|----------------------------------------|
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects`    | Transcripts do Claude Code nesta máquina |
| `CODEX_SESSIONS_DIR`  | `~/.codex/sessions`     | Rollouts do Codex CLI nesta máquina    |
| `DATA_DIR`            | `./data`                | Pasta com os exports das outras máquinas |
| `HOST` / `PORT`       | `127.0.0.1` / `8888`    | Endereço do servidor                   |

O front aceita `?poll=<ms>` na URL para ajustar o intervalo de atualização
automática (padrão 15000 ms).

## Preços

Tabela em `PRICING` no `app.py` (USD por 1M tokens, input/output). Cache: leitura
0,1× input, escrita 1,25× (5m) / 2× (1h). Ajuste se algum modelo mudar de preço.
