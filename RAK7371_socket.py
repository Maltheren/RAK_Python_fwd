import socket
import logging
import sys
import json
import subprocess
import serial.tools.list_ports as lst_ports
import time
import base64
import signal
from typing import Tuple


#Vi tager lige en log med bare fordi
logging.basicConfig(
    level=logging.DEBUG,
    filename='RAK7371_socket.log',
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

log = logging.getLogger(__name__)



###Sets up a port for
class RAK7371:
    lora_socket: socket.socket
    last_ip: Tuple[str, int]

    class FwdHandler:
        driver_proc: subprocess.Popen | None = None 
        @classmethod
        def _setup(cls):
            log.debug("starting lora_pkt_fwd")
            #Starter vores lille driver
            cls.driver_proc = subprocess.Popen( 
                ["./lora_pkt_fwd"],
                cwd="./drivers",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

            start_time = time.time()
            # Læs linjer indtil vi finder det vi leder efter
            for line in cls.driver_proc.stdout:
                print(line, end="")  # viser i terminalen
                if "failed to start the concentrator" in line:
                    cls.driver_proc.terminate()
                    log.error("failed to start the forwarder")
                    break
                if "INFO: [main] concentrator started, packet can now be received" in line:
                    log.info("Sucess, Packets are being forwarded")
                    break
                if time.time() - start_time > 40: ##Hvis vi timeouter prøv at dræbe
                    cls.driver_proc.terminate()
                    log.error("Timeout, killing process")

        @classmethod
        def _nuke(cls):
            """Kills the lora_pkt_fwd execution """
            log.debug("closing lora_pkt_fwd")
            #cls.driver_proc.terminate() #Slår den ihjel igen
        @classmethod
        def is_running(cls):
            """Returns true if the forwarder is currently running"""
            if (cls.driver_proc == None):
                return False
            return True

    @classmethod
    def setup_auto(cls): 
        """Tries to find the COM-port and initate the RAK gateway as a forwarder"""
        port_path = ""
        log.debug("Finding COM-ports")
        for port in lst_ports.comports(True):
            log.debug(port.name)
            if(port.product == 'STM32 Virtual ComPort'):
                port_path = port.name
                log.debug(f"Choose {port.name}")
                break
        if(port_path == ""):
            log.error("No suitable COM-port could be found")
            return False
        return cls.setup_manual(port_path)

    @classmethod
    def setup_manual(cls, path: str):
        """Manually give the path, this will start the forwarder on the gateway"""
        f = open('drivers/global_conf.json', 'r+', encoding='utf-8')
        content = f.read()
        data = json.loads(content)
        data['SX130x_conf']['com_path'] = "/dev/"+ path
        with open('drivers/global_conf.json', 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
        cls.FwdHandler._setup()
        log.debug("Setting up listening socket")
        cls._setup_socket()

    @classmethod
    def _setup_socket(cls):
        """Sets up the socket that listens for the forwarded packages"""
        UDP_IP = "localhost"   # vi lytter lige på der vi forventer at få noget....
        UDP_PORT = 1730
        cls.lora_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        cls.lora_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Vi deler som udgangspunkt med vores rf tool så vi har brug for reuse
        cls.lora_socket.bind((UDP_IP, UDP_PORT))  # Bind to the address and port to listen
        log.info(f"Socket bound to {UDP_IP}:{UDP_PORT}")

    @classmethod
    def _parse_packet(cls, data, addr):
        """Parse used to determine if a valid upstream message have been transmitted."""
        vers = data[0]
        token = data[1:3]
        json_payload: bytes = data[12:]
        if(vers != 2):
            log.error(f"Wrong version, only version 2 supported (Version={vers})")
            return None, None
        cls.last_ip = addr

        if (data[3] == 2): #Hvis vi er igang med en pulldata pakke ACK den pakke
            print("PULLDATA => sending ACK")
            cls.lora_socket.sendto(bytes([0x02, token[0], token[1], 0x04]), addr) #En ACK pakke tilbage til RAK modulet.
            #cls.transmit(b'\x42\x00', addr)

            return token, None
            

        if (len(json_payload) == 0 or data[3] != 0):#Hvis det vi arbejder med ikke er en pakke med noget JSON i
            return token, None

        try:
            json_object = json.loads(json_payload.decode())
            #Såfremt vi fik dekodet noget...
            cls.lora_socket.sendto(bytes([0x02, token[0], token[1], 0x01]), addr) #En ACK pakke tilbage til RAK modulet.
            return token, json_object
        except:
            log.warning(f"Error parsing as JSON package: {json_payload.decode()}")
    
    
    @classmethod
    def poll(cls):
        """Checks if we recieve any valid package from our forwarder"""
        data, addr = cls.lora_socket.recvfrom(1024)
        token, json_obj = cls._parse_packet(data, addr)
        return token, json_obj

    @classmethod
    def _transmit(cls, token, addr, package, freq, pwr, sf, bw, cr):
        log.info("Transmitting")
        tx_json = {
            "txpk": {
                "imme": True,                # send med det samme
                "freq": freq,
                "rfch": 0,
                "powe": pwr,
                "modu": "LORA",
                "datr": f"SF{sf}BW{bw}",
                "codr": cr,
                "ipol": False,
                "size": len(package),
                "data": base64.b64encode(package).decode()
            }
        }
        cmd = json.dumps(tx_json).encode('utf-8')  # Convert to bytes
        ##Så vores packet forwarder svarer på samme port som han sender på... Det skal man lige være OBS på
        down_package = b'\x02'+bytes(token) + b'\x03' + bytes(cmd)
        cls.lora_socket.sendto(down_package ,addr)
        resp = cls.lora_socket.recvfrom(2048)
        if(resp[0] == 0x02 and resp[1:3] == token and resp[3] == 0x05):
            log.info("Package sent")
            log.debug(resp)
        else:
            log.error("Transmit Error")

    @classmethod
    def transmit(cls, package, freq=867.5, pwr: int =20, sf: int=7, bw: int =500, cr: str ="4/5"):
        token=b'\x00\x42'
        cls._transmit(token, cls.last_ip, package, freq, pwr, sf, bw, cr)



def handle_exit(signum, frame):
    log.info("Exiting, cleaing up")
    print(f"cleaning up...")
    RAK7371.FwdHandler._nuke() #Sørger bare for vi lige lukker pænt ned for den driver der kører i baggrunden.
    sys.exit(0)


# If one wants to sniff the sniffed packages
# sudo nast -i lo -f "dst port 1730 or src port 1730"

if __name__ == "__main__":

    signal.signal(signal.SIGINT, handle_exit)   # Ctrl+C
    signal.signal(signal.SIGTERM, handle_exit)  # kill
    print("Setting up RAK7371")
    #RAK7371.setup_auto()
    RAK7371._setup_socket()
    print("Setup complete")
    last_t = time.time()
    while True:
        token, data = RAK7371.poll()
        if (data != None):
            print(data)
        
        if (time.time() - last_t > 5):
            RAK7371.transmit(b'\x42\x42\x42')



