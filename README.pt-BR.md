# OCI Scripts

[English (en-US)](README.md) · **Português (pt-BR)**

Coleção de scripts para automatizar tarefas do dia a dia no Oracle Cloud Infrastructure (OCI).

As instruções de uso, os requisitos e os exemplos estão disponíveis na documentação de cada diretório.

## Aviso legal

Os scripts são fornecidos como estão, sem garantias de disponibilidade, segurança ou adequação a um ambiente específico. Não há compromisso de suporte, manutenção, atualizações ou níveis de serviço.

Os scripts usam as permissões da conta OCI configurada e podem criar, alterar ou excluir recursos e gerar custos. Antes de executar um script, leia sua documentação e revise tenancy, região, compartments, permissões e recursos afetados. Valide o comportamento em um ambiente de testes e mantenha os backups necessários.

O uso e a modificação deste material são de responsabilidade do usuário. Na medida permitida pela legislação aplicável, autores e colaboradores não se responsabilizam por perda de dados, cobranças, indisponibilidade ou outros danos decorrentes do uso. Referências à Oracle e ao OCI não representam endosso ou certificação pela Oracle.

## Scripts disponíveis

| Script | Finalidade | Repositório |
| --- | --- | --- |
| import-custom-images | Importa discos QCOW2 ou VMDK de URLs ou arquivos locais como imagens customizadas, individualmente ou em lotes JSON. Inclui transferências concorrentes, acompanhamento da conversão, retomada, logs e limpeza. | [import-custom-images](import-custom-images/) |

## Como começar

Clone o repositório:

```bash
git clone https://github.com/wuilber002/oci-scripts.git
cd oci-scripts
```

Escolha um script no catálogo e entre em seu diretório, substituindo `DIRETORIO_DO_SCRIPT` pelo nome do diretório correspondente:

```bash
cd DIRETORIO_DO_SCRIPT
```

Leia o README daquele diretório para consultar requisitos, permissões, parâmetros e exemplos de execução.
