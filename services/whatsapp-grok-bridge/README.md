# Ponte de leitura e envio para um WhatsApp

Serviço local. Ele não responde sozinho e não dispara campanha. Uma mensagem
sai só quando alguém chama `POST /v1/messages`.

A chave administrativa do CRM e a chave do WAHA não saem deste servidor. O
cliente externo usa outro segredo, o `BRIDGE_TOKEN`, no cabeçalho
`Authorization: Bearer <BRIDGE_TOKEN>`.

O WAHA instalado (2026.7.2 CORE) trata a permissão `send` como um conjunto
amplo: além de enviar texto, ela libera grupos, contatos, perfil, etiquetas,
canais, status e alterações de conversa. Esta ponte repassa grupos, contatos,
etiquetas, status, conversas e o envio de mensagens, sempre na sessão
configurada em `WAHA_SESSION`. Perfil, canais, logout e gestão da sessão ficam
de fora.

## Endereço

Enquanto a ponte escuta só em `127.0.0.1:3011` na VPS, o acesso de fora é um
túnel SSH:

```powershell
ssh -N -L 127.0.0.1:3011:127.0.0.1:3011 root@89.116.73.110
```

Base: `http://127.0.0.1:3011`

O túnel serve para um programa rodando neste computador. O Grok da xAI, no
computador e no celular, usa outro endereço, com cadeado, no mesmo site do CRM:

`https://crm.verticesales.com.br/wa-assistente/mcp`

Esse endereço só abre o conector. O painel do WhatsApp continua fechado.
No Grok: grok.com/connectors, Novo conector, Personalizado. A URL do servidor
é a de cima. Na tela de OAuth:

| campo | valor |
|---|---|
| ID do Cliente | `grok` |
| Segredo do Cliente | deixe vazio |
| Endpoint de Autorização | `https://crm.verticesales.com.br/wa-assistente/oauth/authorize` |
| Endpoint do Token | `https://crm.verticesales.com.br/wa-assistente/oauth/token` |
| Escopos | deixe vazio |
| Método | nenhum (somente PKCE) |

Salvar abre uma página pedindo o `BRIDGE_TOKEN`. O conector fica na conta, então o celular passa a enxergar as mesmas ferramentas.

## Credencial

No seu computador, o comando abaixo mostra o token só no seu terminal:

```powershell
ssh root@89.116.73.110 "grep '^BRIDGE_TOKEN=' /root/deskcommcrm/.waha-grok-bridge.env"
```

Nos exemplos, troque `<BRIDGE_TOKEN>` por esse valor. Não cole o token em chat.

## Ler o histórico

O histórico vem das mensagens que o CRM já guardou (recebidas e enviadas,
inclusive as que saíram pelo aplicativo do celular, marcadas como
`sent_via=external_device`). O WAHA desta instalação está no motor NOWEB sem
a store ligada, então `GET /api/{sessão}/chats` não devolve o arquivo antigo.

```powershell
curl.exe -s -H "Authorization: Bearer <BRIDGE_TOKEN>" http://127.0.0.1:3011/v1/messages
```

```powershell
curl.exe -s -H "Authorization: Bearer <BRIDGE_TOKEN>" http://127.0.0.1:3011/v1/session
```

A lista traz no máximo as 50 mensagens mais recentes. Para ouvir um áudio, use `whatsapp_audio` com o id da mensagem. Para ver uma foto ou um print, use `whatsapp_imagem` com o id.

## Grupos, contatos, etiquetas e status

Estes caminhos repetem a API do WAHA, presos à sessão configurada. O cliente não escolhe outra sessão: se o JSON trouxer `session`, a ponte substitui pelo número autorizado.

```powershell
curl.exe -s -H "Authorization: Bearer <BRIDGE_TOKEN>" http://127.0.0.1:3011/v1/groups
curl.exe -s -H "Authorization: Bearer <BRIDGE_TOKEN>" http://127.0.0.1:3011/v1/contacts/all
curl.exe -s -H "Authorization: Bearer <BRIDGE_TOKEN>" http://127.0.0.1:3011/v1/labels
curl.exe -s -H "Authorization: Bearer <BRIDGE_TOKEN>" "http://127.0.0.1:3011/v1/status/new-message-id"
```

Nesta instalação o motor é NOWEB e a store está desligada. Grupos e o identificador de status respondem. A lista de contatos do WhatsApp e as etiquetas devolvem erro pedindo `config.noweb.store.enabled` e `full_sync` na criação da sessão. Ligar isso reinicia a sessão do WhatsApp (ela volta a conectar sozinha, sem QR, e fica alguns instantes fora do ar). O histórico que a ponte já entrega continua sendo o do CRM.

Enviar um texto também pode ir pelo mesmo verbo do WAHA. Sem `chatId` e `text` nada é entregue:

```powershell
curl.exe -s -X POST http://127.0.0.1:3011/v1/sendText ^
  -H "Authorization: Bearer <BRIDGE_TOKEN>" ^
  -H "Content-Type: application/json" ^
  -d "{\"chatId\":\"5511999999999@c.us\",\"text\":\"Texto autorizado por mim\"}"
```

Publicar um status (`POST /v1/status/text`) ou alterar um grupo só deve acontecer quando você pedir. A ponte não faz isso sozinha.

## Enviar um texto pelo atalho

Só número direto, com DDI e DDD, só dígitos. Não envia para grupo.

```powershell
curl.exe -s -X POST http://127.0.0.1:3011/v1/messages ^
  -H "Authorization: Bearer <BRIDGE_TOKEN>" ^
  -H "Content-Type: application/json" ^
  -d "{\"to\":\"5511999999999\",\"text\":\"Texto autorizado por mim\"}"
```

Um envio real para um cliente não faz parte da validação. Use um número de
teste que você indicar.

## O que esta ponte recusa

- outra sessão ou outro número de origem
- logout, start, stop, restart
- apagar a sessão, mudar a configuração dela, gerenciar chaves
- perfil do WhatsApp e qualquer caminho fora dos recursos acima
