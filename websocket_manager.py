# backend/websocket_manager.py
from fastapi import WebSocket
from typing import Dict, List


class ConnectionManager:
    def __init__(self):
        # Dictionary to store active connections:
        # Key = username, Value = List of WebSockets (allowing multiple tabs/devices)
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, username: str):
        """Accepts a new connection and adds it to the list."""
        await websocket.accept()

        if username not in self.active_connections:
            self.active_connections[username] = []

        self.active_connections[username].append(websocket)
        print(f"User {username} connected. Active sessions: {len(self.active_connections[username])}")

    def disconnect(self, websocket: WebSocket, username: str):
        """Removes a connection from the list."""
        if username in self.active_connections:
            if websocket in self.active_connections[username]:
                self.active_connections[username].remove(websocket)

            # If user has no more open tabs, remove them from the dict entirely
            if not self.active_connections[username]:
                del self.active_connections[username]

        print(f"User {username} disconnected.")

    async def broadcast_to_participants(self, participants: list[str], message: dict, sender: str):
        for participant in participants:
            if participant in self.active_connections:
                # Get the list of sockets for this user (e.g. Phone + Laptop)
                user_connections = self.active_connections[participant]

                # Iterate over ALL their open connections
                for websocket in user_connections:
                    try:
                        await websocket.send_json(message)
                    except Exception as e:
                        # Handle case where one specific socket might be dead
                        print(f"Error sending to {participant}: {e}")


# Create a global instance to be imported elsewhere
manager = ConnectionManager()