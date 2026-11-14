# Dashboards só do Grafana da VM (tcf3...:3000, pasta ToggleMaster F4)

Dashboards que **não** vão para o Grafana do cluster: usam datasources que só existem na VM
(por exemplo o Azure Monitor). Não entram no `kustomization.yaml` desta pasta-mãe, então o Argo CD
não os publica.

Importados pelo `../scripts/sync-grafana-vm.py`, junto com os de `../dashboards/`.

| Arquivo | Dashboard |
|---|---|
| `tm-f4-cicd.json` | ToggleMaster F4 · VM de CI/CD (Jenkins): disponibilidade/despejos Spot, CPU, memória, disco e rede da `vm-tcf3` via Azure Monitor |

Fonte da verdade é este repositório: edição feita na tela da VM é sobrescrita na próxima sincronização.
