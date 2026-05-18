import asyncio
import websockets
import json

async def test_client():
    uri = "ws://localhost:8000/ws"
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected")
            # Send the initialization message
            await websocket.send(json.dumps({
                "type": "start",
                "videoUrl": "https://youtube.com/watch?v=123"
            }))
            print("Sent start")
            # Send a fake audio chunk
            await websocket.send(b"fake audio data")
            print("Sent audio chunk")
            # Wait a bit
            await asyncio.sleep(2)
            # Receive response
            try:
                response = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                print(f"Received: {response}")
            except Exception as e:
                print(f"No response: {e}")
    except Exception as e:
        print(f"Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_client())
