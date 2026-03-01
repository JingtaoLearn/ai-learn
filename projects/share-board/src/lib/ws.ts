type MessageHandler = (data: Record<string, unknown>) => void;

export class BoardWebSocket {
  private ws: WebSocket | null = null;
  private onMessage: MessageHandler;
  private boardId: string;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private viewerId: string | null = null;

  constructor(boardId: string, onMessage: MessageHandler) {
    this.boardId = boardId;
    this.onMessage = onMessage;
    this.connect();
  }

  private connect() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    let url = `${protocol}//${window.location.host}/ws?boardId=${this.boardId}`;
    if (this.viewerId) {
      url += `&viewerId=${this.viewerId}`;
    }
    this.ws = new WebSocket(url);

    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "viewer-id" && msg.viewerId) {
          this.viewerId = msg.viewerId;
        }
        this.onMessage(msg);
      } catch {
        // Ignore malformed messages
      }
    };

    this.ws.onclose = () => {
      this.reconnectTimer = setTimeout(() => this.connect(), 3000);
    };
  }

  sendUpdate(editToken: string, data: string) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(
        JSON.stringify({ type: "update", editToken, data }),
      );
    }
  }

  sendLaser(editToken: string, data: string) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(
        JSON.stringify({ type: "laser", editToken, data }),
      );
    }
  }

  sendEditRequest() {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "edit-request" }));
    }
  }

  sendEditResponse(editToken: string, viewerId: string, approved: boolean) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(
        JSON.stringify({ type: "edit-response", editToken, viewerId, approved }),
      );
    }
  }

  close() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
    this.ws = null;
  }
}
