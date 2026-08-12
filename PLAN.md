# Plano — Orquestrador de Agentes com LangGraph + OpenCode

## 1. Objetivo

Construir um orquestrador local que receba GitHub Issues de múltiplos repositórios e conduza automaticamente o ciclo:

```text
GitHub Issue
    ↓
LangGraph
    ↓
Resolve repository
    ↓
Clone / fetch repository
    ↓
Create isolated git worktree + branch
    ↓
OpenCode (plan)
    ↓
OpenCode (build)
    ↓
Tests
    ↓
OpenCode (review)
    ↓
Fix loop, if necessary
    ↓
Create GitHub PR
```

O LangGraph será responsável pelo workflow, estado, decisões e retomadas. O OpenCode continuará sendo o executor dos trabalhos de desenvolvimento, usando os agentes `plan`/`build` e as skills já existentes.

---

## 2. Princípios da arquitetura

### LangGraph não será um substituto do OpenCode

O LangGraph não terá agentes especializados próprios.

Ele deverá apenas orquestrar etapas como:

- `prepare_workspace`
- `plan`
- `implement`
- `test`
- `review`
- `fix`
- `create_pr`
- `ask_human`
- `cleanup`

Cada etapa que exigir raciocínio ou alteração de código chama o OpenCode.

Exemplo:

```text
LangGraph
    │
    ├── plan_node
    │      └── opencode run --agent plan
    │
    ├── implement_node
    │      └── opencode run --agent build
    │
    ├── review_node
    │      └── opencode run --agent plan
    │
    └── fix_node
           └── opencode run --agent build
```

---

# 3. Estrutura de diretórios

O orquestrador fica separado dos repositórios:

```text
~/agent-orchestrator/
├── pyproject.toml
├── README.md
├── src/
│   └── orchestrator/
│       ├── __init__.py
│       ├── graph.py
│       ├── state.py
│       ├── github.py
│       ├── git.py
│       ├── opencode.py
│       ├── workspace.py
│       ├── config.py
│       └── main.py
├── tests/
└── data/
    └── state/
```

Os repositórios trabalhados ficam em outro diretório:

```text
~/agent-workspaces/
├── task-123/
│   └── backend/
├── task-456/
│   └── frontend/
└── task-789/
    └── backend/
```

Não usar um único checkout compartilhado.

Cada execução terá seu próprio worktree.

---

# 4. Isolamento por task

Cada GitHub Issue que entrar no sistema deverá gerar uma execução independente.

Exemplo:

```text
company/backend#123
company/backend#456
company/frontend#87
```

vira:

```text
~/agent-workspaces/
├── company-backend-123/
├── company-backend-456/
└── company-frontend-87/
```

Cada task terá uma branch própria:

```text
ai/issue-123
ai/issue-456
ai/issue-87
```

Isso permite executar tasks simultaneamente sem que os agentes compartilhem arquivos modificados.

---

# 5. Estado do LangGraph

Começar com um estado semelhante a:

```python
class TaskState(TypedDict):
    task_id: str

    repository: str
    issue_number: int
    issue_title: str
    issue_body: str

    repository_url: str
    base_branch: str
    branch: str
    workspace: str

    plan: str | None
    implementation_result: str | None
    test_result: str | None
    review_result: str | None

    status: str
    question: str | None

    pr_number: int | None
    error: str | None

    iteration: int
```

O estado deve ser suficiente para retomar uma execução interrompida.

---

# 6. Estados da task

Usar estados explícitos:

```text
RECEIVED
PREPARING
PLANNING
WAITING_FOR_HUMAN
IMPLEMENTING
TESTING
REVIEWING
FIXING
CREATING_PR
COMPLETED
FAILED
```

Fluxo principal:

```text
RECEIVED
   ↓
PREPARING
   ↓
PLANNING
   ↓
IMPLEMENTING
   ↓
TESTING
   ↓
REVIEWING
   ├── needs clarification → WAITING_FOR_HUMAN
   ├── changes needed      → FIXING → TESTING
   └── approved             → CREATING_PR
                                  ↓
                              COMPLETED
```

---

# 7. Preparação do workspace

O primeiro passo real da execução será descobrir e preparar o repositório correto.

Entrada:

```text
repository = company/backend
issue = #123
```

O orchestrator deverá:

1. Validar que o repositório existe.
2. Obter a URL do clone.
3. Obter a branch padrão.
4. Criar um diretório específico para a task.
5. Clonar o repositório, caso ainda não exista.
6. Fazer `fetch` das referências necessárias.
7. Criar uma branch de trabalho.
8. Criar o git worktree.
9. Armazenar o caminho no `TaskState`.

Exemplo:

```text
GitHub
  │
  ▼
company/backend#123
  │
  ▼
~/agent-workspaces/company-backend-123/
  │
  ▼
branch: ai/issue-123
```

---

# 8. Estratégia Git

Preferir `git worktree` para isolamento.

Exemplo conceitual:

```bash
git clone git@github.com:company/backend.git ~/agent-repos/backend

cd ~/agent-repos/backend

git fetch origin

git worktree add   ~/agent-workspaces/company-backend-123   -b ai/issue-123   origin/main
```

Para múltiplas tasks do mesmo repositório:

```text
backend.git
   │
   ├── worktree → issue-123
   ├── worktree → issue-456
   └── worktree → issue-789
```

O clone base pode ser reutilizado enquanto os worktrees permanecem isolados.

---

# 9. Execução do OpenCode

Criar um wrapper único:

```python
run_opencode(
    workspace=state["workspace"],
    agent="plan",
    prompt=...
)
```

Inicialmente utilizar a CLI:

```bash
opencode run --agent plan "..."
```

e:

```bash
opencode run --agent build "..."
```

O wrapper deve:

- executar no workspace correto;
- capturar stdout/stderr;
- capturar exit code;
- aplicar timeout;
- retornar resultado estruturado;
- registrar logs;
- detectar falhas.

Não integrar profundamente com APIs internas do OpenCode na primeira versão.

---

# 10. Planning

O node `plan` deve executar OpenCode com o agente `plan`.

Prompt deve informar:

- issue;
- repository;
- branch;
- objetivo;
- restrições;
- que não deve criar PR;
- que deve usar as skills disponíveis apropriadas.

Exemplo conceitual:

```text
Analyze GitHub issue #123.

Repository: company/backend

Issue:
<issue body>

Use the plan-implementation skill.

Produce:
1. Requirements
2. Implementation plan
3. Files likely to change
4. Tests required
5. Potential risks
6. Questions if requirements are ambiguous

Do not modify the repository.
```

O resultado deverá ser armazenado em `state.plan`.

---

# 11. Human-in-the-loop

Se o planejamento detectar ambiguidade:

```text
PLANNING
   ↓
needs clarification
   ↓
WAITING_FOR_HUMAN
```

O orchestrator publica um comentário na Issue:

```text
The implementation requires clarification:

Should the cache be invalidated when the user is updated?
```

A execução fica pausada.

Quando um novo comentário for recebido:

```text
GitHub webhook
    ↓
issue_comment
    ↓
find task
    ↓
resume LangGraph execution
```

O novo comentário deve ser incorporado ao contexto antes de continuar.

---

# 12. Implementação

Depois de um plano aprovado:

```text
IMPLEMENTING
```

Executar:

```text
opencode run --agent build
```

O prompt deve incluir:

- Issue original;
- plano;
- respostas dadas pelo usuário;
- instrução para usar `subagent-plan-execution`;
- instrução para implementar;
- instrução para executar testes;
- instrução para não criar PR.

O agente trabalha somente dentro do worktree daquela task.

---

# 13. Testes

Após implementação, o orchestrator deve executar os testes apropriados.

Na primeira versão, permitir que o próprio OpenCode determine e execute os testes:

```text
opencode --agent build
```

Depois pode ser adicionada configuração por repositório:

```yaml
commands:
  test: ./gradlew test
  lint: ./gradlew ktlintCheck
```

ou:

```yaml
commands:
  test: npm test
  lint: npm run lint
```

O resultado deve ser salvo no estado.

---

# 14. Review

Executar novamente o OpenCode, preferencialmente com `plan`, para revisar a implementação.

O reviewer deverá analisar:

- issue original;
- plano;
- diff;
- testes;
- aderência aos requisitos;
- possíveis regressões.

Resultado estruturado:

```text
APPROVED
```

ou:

```text
CHANGES_REQUIRED
```

ou:

```text
NEEDS_CLARIFICATION
```

---

# 15. Fix loop

Se a revisão solicitar alterações:

```text
REVIEWING
    ↓
CHANGES_REQUIRED
    ↓
FIXING
    ↓
TESTING
    ↓
REVIEWING
```

Definir um limite inicial:

```text
MAX_ITERATIONS = 3
```

Se atingir o limite:

```text
FAILED
```

ou:

```text
WAITING_FOR_HUMAN
```

A decisão pode ser configurável posteriormente.

---

# 16. Criação do Pull Request

Somente depois de:

- implementação concluída;
- testes passando;
- review aprovado;

executar:

```text
CREATING_PR
```

O orchestrator deverá:

1. verificar o diff;
2. garantir que não existem alterações inesperadas;
3. commit;
4. push da branch;
5. criar PR;
6. adicionar referência à Issue;
7. armazenar `pr_number`.

Branch:

```text
ai/issue-123
```

PR:

```text
feat: add Redis cache

Closes #123
```

---

# 17. Webhook do GitHub

Criar um pequeno servidor HTTP para receber:

```text
issues
issue_comment
pull_request
```

Inicialmente somente:

```text
issues.opened
issues.reopened
issue_comment.created
```

serão necessários.

Fluxo:

```text
GitHub
   │
   │ webhook
   ▼
FastAPI
   │
   ▼
LangGraph
```

Não deixar o webhook executar diretamente o agente.

Ele deve apenas:

1. validar evento;
2. localizar/criar task;
3. iniciar ou retomar a execução do grafo.

---

# 18. Múltiplos repositórios

O orchestrator deve tratar o nome completo do repositório como identificador:

```text
owner/repository
```

Exemplos:

```text
company/backend
company/frontend
company/mobile
```

A task sempre terá:

```text
repository
issue_number
workspace
branch
```

Assim o LangGraph nunca precisa assumir que está trabalhando em um único projeto.

---

# 19. Concorrência

Não implementar concorrência complexa na primeira versão.

Depois que o fluxo funcionar, permitir:

```text
Task #123 ── OpenCode process 1
Task #456 ── OpenCode process 2
Task #789 ── OpenCode process 3
```

Cada task terá:

- state próprio;
- workspace próprio;
- branch própria;
- processo OpenCode próprio.

Adicionar um limite global:

```text
MAX_CONCURRENT_TASKS=2
```

Isso também permite controlar consumo de API.

---

# 20. Persistência

O LangGraph deve ter checkpoint/persistência para que uma execução não desapareça quando o processo for reiniciado.

Começar com uma solução simples local.

Posteriormente:

```text
LangGraph
    ↓
PostgreSQL
```

O PostgreSQL também pode armazenar metadados complementares:

```text
tasks
repositories
workspaces
agent_runs
```

---

# 21. Configuração por repositório

Somente depois do MVP, adicionar configuração:

```yaml
repositories:
  - name: company/backend
    default_branch: main
    test_command: ./gradlew test

  - name: company/frontend
    default_branch: main
    test_command: npm test
```

Possivelmente permitir:

```yaml
repositories:
  - name: company/backend
    agent:
      model: deepseek
    commands:
      test: ./gradlew test
      lint: ./gradlew ktlintCheck
```

Não colocar conhecimento específico dos repositórios no código do orchestrator.

---

# 22. Segurança

O OpenCode terá capacidade de:

- modificar arquivos;
- executar comandos;
- acessar Git;
- potencialmente acessar credenciais.

Por isso:

- cada task deve ter workspace isolado;
- limitar permissões quando possível;
- não expor secrets desnecessariamente ao agente;
- não executar comandos arbitrários vindos diretamente do conteúdo da Issue;
- validar repository/owner antes de clonar;
- evitar que uma Issue possa escolher arbitrariamente um caminho local;
- usar credenciais GitHub com permissões mínimas.

---

# 23. Observabilidade

Cada execução deverá gerar logs:

```text
task_id
repository
issue
node
start_time
end_time
exit_code
OpenCode output
```

Estrutura:

```text
logs/
└── task-123/
    ├── plan.log
    ├── implementation.log
    ├── test.log
    └── review.log
```

No futuro, migrar para logs estruturados.

---

# 24. MVP

O primeiro MVP deve fazer somente:

```text
GitHub Issue
    ↓
Prepare workspace
    ↓
OpenCode plan
    ↓
OpenCode build
    ↓
Run tests
    ↓
OpenCode review
    ↓
Create PR
```

Com:

- um único repositório ou poucos repositórios;
- múltiplos workspaces;
- branches isoladas;
- LangGraph;
- OpenCode CLI;
- GitHub API;
- persistência básica;
- máximo de 1 task concorrente inicialmente.

Não implementar inicialmente:

- dashboard;
- Kubernetes;
- múltiplos modelos;
- configuração sofisticada por repositório;
- agentes especializados;
- memória de longo prazo;
- sistema complexo de filas.

---

# 25. Evolução após o MVP

## V2

Adicionar:

- human-in-the-loop;
- clarification via GitHub comments;
- review/fix loop;
- múltiplas tasks concorrentes;
- limite de concorrência;
- configuração por repository.

## V3

Adicionar:

- PostgreSQL;
- execução persistente;
- retries;
- métricas;
- melhor gerenciamento de worktrees;
- cleanup automático.

## V4

Adicionar:

- deployment no k3s;
- workers separados;
- fila de tasks;
- sandbox por execução;
- dashboard;
- múltiplos providers/modelos.

Arquitetura futura:

```text
                         GitHub
                            │
                         Webhook
                            │
                            ▼
                    ┌──────────────┐
                    │ Orchestrator │
                    │  LangGraph   │
                    └──────┬───────┘
                           │
                        Queue
                           │
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
          Worker 1      Worker 2      Worker 3
             │             │             │
          OpenCode      OpenCode      OpenCode
             │             │             │
         Worktree      Worktree      Worktree
             │             │             │
             └─────────────┼─────────────┘
                           │
                           ▼
                         GitHub
```

---

# 26. Ordem concreta de implementação

1. Criar projeto Python com `uv`.
2. Instalar LangGraph.
3. Criar `TaskState`.
4. Criar grafo `START → PLAN → BUILD → TEST → REVIEW → END`.
5. Implementar wrapper `opencode.py`.
6. Testar com um repositório local.
7. Implementar gerenciamento de worktree.
8. Fazer o orchestrator receber `owner/repo + issue`.
9. Implementar clone/fetch automático.
10. Criar branch automática.
11. Passar workspace correto para cada execução do OpenCode.
12. Implementar GitHub API.
13. Criar webhook.
14. Implementar criação automática do PR.
15. Adicionar persistência/checkpoints.
16. Adicionar `WAITING_FOR_HUMAN`.
17. Adicionar review/fix loop.
18. Adicionar concorrência limitada.
19. Adicionar configuração por repositório.
20. Só então considerar deployment no k3s.

---

# 27. Resultado esperado

Ao final, o uso deverá ser essencialmente:

```text
Developer
    │
    ▼
GitHub Issue
    │
    ▼
[automático]
    │
    ├── identifica repository
    ├── cria workspace
    ├── cria branch
    ├── planeja
    ├── implementa
    ├── testa
    ├── revisa
    ├── pede esclarecimento se necessário
    └── cria PR
```

O developer não precisa iniciar manualmente o OpenCode.

O OpenCode continua sendo o agente de desenvolvimento.

O LangGraph vira a camada responsável por transformar uma Issue em uma execução completa e persistente de engenharia de software.
