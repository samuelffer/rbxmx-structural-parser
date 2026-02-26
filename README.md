# rbxbundle

**Roblox Bundle Extractor (bundle-only)**  
Extrai e estrutura arquivos `.rbxmx/.xml/.txt` contendo XML de modelos Roblox e gera um bundle organizado com hierarquia, índice e scripts separados.

---

## 📦 Requisitos

- Python **3.9+**
- Nenhuma biblioteca externa necessária

---

## 🚀 Como usar

### 1) Coloque o arquivo na pasta
```
input/
```

Extensões suportadas:
```
.rbxmx
.xml
.txt
```

---

### 2) Execute
```bash
python rbxmx_bundle.py
```

---

### 3) Escolha
- arquivo listado
- gerar ou não `CONTEXT.txt`

---

## 📂 Saída gerada

Dentro de:

```
output/
```

Serão criados:

```
<nome>_bundle/
<nome>_bundle.zip
```

---

## 📑 Conteúdo do bundle

### HIERARCHY.txt
Hierarquia textual completa do modelo:

```
- Nome (Classe)
  - Filho (Classe)
```

---

### INDEX.csv
Tabela com todos scripts detectados

| Coluna | Descrição |
|------|-----------|
class | tipo do script |
name | nome |
path | caminho hierárquico |
file | nome exportado |
source_len | tamanho do source |

---

### CONTEXT.txt *(opcional)*

Lista objetos estruturais importantes:

- RemoteEvent
- RemoteFunction
- BindableEvent
- BindableFunction
- ValueObjects
- Folder
- Configuration

---

### scripts/

Export automático:

| Tipo | Extensão |
|-----|----------|
Script | `.server.lua` |
LocalScript | `.client.lua` |
ModuleScript | `.lua` |

Cada script inclui header com:

- Class  
- Name  
- Path  

---

## ⚙️ Detalhes técnicos

O parser:

- Detecta `DataModel` raiz
- Percorre todos `<Item>`
- Sanitiza nomes de arquivo
- Ignora scripts sem Source válido
- Gera ZIP automaticamente

---

## ⚠️ Limitações

- Não analisa dependências internas
- Não resolve requires
- Não detecta Attributes ainda
- XML inválido pode falhar

---

## 🗺️ Roadmap

- CLI com argumentos (`--in`, `--out`)
- Grafo de dependências
- Export JSON index
- Detector de scripts vazios
- Mapa de remotes
- Parser de atributos

---

## 📜 Licença
MIT License

---

## 👤 Autor
**@samuelffer**
