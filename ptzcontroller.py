import cv2, time, argparse, time
import pyopenpose as op
import paho.mqtt.client as mqtt
import os, fcntl
import v4l2
from enum import IntEnum
from signal import signal, SIGINT
from sys import exit
from movecontrol import Move, ViscaMoveControl
from videosource import CaptureVideoSource, NDIVideoSource


class Edge(IntEnum):
    LEFT = 0
    TOP = 1
    RIGHT = 2
    BOTTOM = 3


class PTZTrack:
    control_camera = None

    def __init__(self, args):
        self.args = args
        self.move = ViscaMoveControl(self.args)
        self.zooming = False
        self.odevice = open("/dev/video1", "wb")
        
        # V4L2 Setup
        cap                         = cv2.VideoCapture(0)
        ret, im                     = cap.read()
        height2, width2, channels2  = im.shape

        format                      = v4l2.v4l2_format()
        format.type                 = v4l2.V4L2_BUF_TYPE_VIDEO_OUTPUT
        format.fmt.pix.field        = v4l2.V4L2_FIELD_NONE
        format.fmt.pix.pixelformat  = v4l2.V4L2_PIX_FMT_BGR24
        format.fmt.pix.width        = 1280
        format.fmt.pix.height       = 720
        format.fmt.pix.bytesperline = 1280 * channels2
        format.fmt.pix.sizeimage    = 1280 * 720 * channels2

        print ("set format result (0 is good):{}".format(fcntl.ioctl(self.odevice, v4l2.VIDIOC_S_FMT, format)))

        # OpenPose Setup
        params = dict()
        params["model_folder"] = "/openpose/models"
        params["net_resolution"] = self.args.net_resolution

        self.openpose = op.WrapperPython()
        self.openpose.configure(params)
        self.openpose.start()

        # Video Source Setup
        if self.args.video_source == "device":
            self.video_source = CaptureVideoSource(args)
        elif self.args.video_source == "ndi":
            self.video_source = NDIVideoSource(args)
        else:
            print("Invalid video source provided.")
            exit()

        # MQTT Setup
        if self.args.mqtt:
            self.mqttc = mqtt.Client("PTZTrack")
            self.mqttc.connect(self.args.mqtt_host)
            self.mqttc.loop_start()
            self.mqttc.subscribe("PTZ_SETSTATE")
            self.mqttc.on_message = self.mqtt_message
            self.mqtt_publish_state()
            print("MQTT Connected")
        else:
            print("Skipping MQTT Connection")

        signal(SIGINT, self.sigint_handler)

    def mqtt_message(self, client, userdata, message):
        data = message.payload.decode("utf-8")
        print("Control message:", data)

        if data.startswith("control state"):
            self.mqtt_publish_state()

        elif data.startswith("control on"):
            self.control_camera = True
            self.mqtt_publish_state()

        elif data.startswith("control off"):
            self.control_camera = False
            self.mqtt_publish_state()

        elif data.startswith("control toggle"):
            self.control_camera = not self.control_camera
            self.mqtt_publish_state()

    def calculate_pan_speed(self, smin: int, val: int, smax: int):
        speed_ratio = (val - smin) / smax
        speed = int(
            ((self.args.speed_max - self.args.speed_min) * speed_ratio)
            + self.args.speed_min
        )
        return speed
    def calculate_tilt_speed(self, smin: int, val: int, smax: int):
        speed_ratio = (val - smin) / smax
        speed = int(
            ((self.args.speed_max - (self.args.speed_min * 1.75)) * speed_ratio)
            + self.args.speed_min
        )
        return speed

    def move_state_str(self):
        return "on" if self.control_camera else "off"

    def get_keypoints_rectangle(self, keypoints, threshold=0.2):
        numberKeypoints = keypoints.shape[0]
        if numberKeypoints < 1:
            return "Number body parts must be > 0."

        minX = minY = float("inf")
        maxX = maxY = float("-inf")

        for keypoint in keypoints:
            score = keypoint[2]
            if score > threshold:
                x = keypoint[0]
                y = keypoint[1]

                if maxX < x:
                    maxX = x
                if minX > x:
                    minX = x

                if maxY < y:
                    maxY = y
                if minY > y:
                    minY = y

        if maxX >= minX and maxY >= minY:
            return int(minX), int(minY), int(maxX), int(maxY)

    def sigint_handler(self, signal_received, frame):
        print("Program exit requested... Exiting gracefully")
        self.control_camera = False
        if self.args.mqtt:
            self.mqtt_publish_state()
            time.sleep(0.1)
            self.mqttc.loop_stop()
        self.video_source.close()
        cv2.destroyAllWindows()
        exit(0)

    def read_frame(self):
        ret, frame = self.video_source.frame_read()
        if ret and (frame.shape[0] != 720) and (frame.shape[1] != 1280):
            frame = cv2.resize(frame, (1280, 720))
        return ret, frame

    def mqtt_publish_state(self):
        self.mqttc.publish("PTZ_STATE", self.move_state_str())

    def show_ui(self, frame):
        cv2.imshow("PTZTrack Frame", frame)

        if cv2.waitKey(25) & 0xFF == ord("q"):
            return True

    def calculate_edges(self, frame_shape):
        l_edge = int(frame_shape[1] * self.args.boundary)
        r_edge = frame_shape[1] - l_edge
        height = frame_shape[0]
        width = frame_shape[1]
        lower_edge = int(frame_shape[0] * .45)
        upper_edge = frame_shape[0] - lower_edge

        bounding = [frame_shape[1], height, 0, 0]

        return l_edge, r_edge, height, width, lower_edge, upper_edge, bounding

    def process_datum_keypoints(self, frame, datum):
        regions = []

        if datum.poseKeypoints is not None and len(datum.poseKeypoints) > 0:
            for i in range(0, datum.poseKeypoints.shape[0]):
                p = self.get_keypoints_rectangle(datum.poseKeypoints[i], 0.1)
                regions.append([p[0], p[1], p[2] - p[0], p[3] - p[1]])
                cv2.rectangle(frame, (p[0], p[1]), (p[2], p[3]), (0, 255, 255), 2)

        return frame, regions

    def calculate_boundaries(self, bounding, regions):
        for (x, y, w, h) in regions:
            if x < bounding[Edge.LEFT]:
                bounding[Edge.LEFT] = x
            if y < bounding[Edge.TOP]:
                bounding[Edge.TOP] = y
            if x + w > bounding[Edge.RIGHT]:
                bounding[Edge.RIGHT] = x + w
            if y + h > bounding[Edge.BOTTOM]:
                bounding[Edge.BOTTOM] = y + h

        return bounding

    
    
    def main_loop(self):
        bounding = []
        val1   = 10
        val2   = 10
        val3   = 10
        val4   = 10
        val5   = 10
        val6   = 10
        val7   = 10
        val8   = 10
        val9   = 10
        val10  = 10


        self.move.set_direction(Move.STOP)
        last_direction = self.move.direction

        self.move.set_speed(self.args.speed_min)
        last_speed = self.move.speed

        frame_count = 0

        while self.video_source.source_available():
            check, frame = self.read_frame()

            # If there is no video data
            if not check:
                # sleep momentarily so we can't waste time in an endless loop
                print("No Video Data!")
                time.sleep(0.01)
                continue


            # Publish control state occasionally
            frame_count += 1
            if self.args.mqtt and frame_count == 20:
                self.mqtt_publish_state()
                frame_count = 0

            # Don't spend time processing if we're not going to control the camera
            if not self.control_camera:
                continue

            l_edge, r_edge, height, width, lower_edge, upper_edge, bounding = self.calculate_edges(frame.shape)
##            lower_edge = (frame_shape[0] * 0.45)
##            upper_edge = (frame_shape[0] - lower_edge)
            

            # Pass the frame data to openpose
            openpose_datum = op.Datum()
            openpose_datum.cvInputData = frame
            self.openpose.emplaceAndPop(op.VectorDatum([openpose_datum]))
            frame = openpose_datum.cvOutputData

            # Actually get openpose to process the keypoints
            frame, regions = self.process_datum_keypoints(frame, openpose_datum)

            if len(regions) > 0:
                # calculate the bounding boxes of all the people
                bounding = self.calculate_boundaries(bounding, regions)

                val1 = val2
                val2 = val3
                val3 = val4
                val4 = val5
                val5 = val6
                val6 = val7
                val7 = val8
                val8 = val9
                val9 = val10
                val10 = (bounding[Edge.TOP] - bounding[Edge.BOTTOM])
                val11 = (val6 + val7 + val8 + val9 + val10) / 5 
                val11 = abs(val11)
                recty1 = (height - val11) * 0.5
                recty2 = recty1 + val11
                cv2.rectangle(
                    frame, 
                    (20, int(recty1)), 
                    (20, int(recty2)), 
                    (255, 255, 0),
                    4,
                )

                # Calculate the middle of the box
                lrmiddle = int(
                    ((bounding[Edge.RIGHT] - bounding[Edge.LEFT]) / 2)
                    + bounding[Edge.LEFT]
                )
                udmiddle = int(
                    ((bounding[Edge.TOP] - bounding[Edge.BOTTOM]) / 2)
                    + bounding[Edge.BOTTOM]
                )

                # Draw the bounding boxes and middle point
                cv2.rectangle(
                    frame,
                    (bounding[Edge.LEFT], bounding[Edge.TOP]),
                    (bounding[Edge.RIGHT], bounding[Edge.BOTTOM]),
                    (0, 200, 0),
                    2,
                )
                cv2.rectangle(
                    frame,
                    (lrmiddle - 1, udmiddle - 1),
                    (lrmiddle + 1, udmiddle + 1),
                    (255, 255, 0),
                    4,
                )
                cv2.rectangle(frame, (l_edge, 0), (r_edge, height), (255, 0, 0), 4)
                cv2.rectangle(frame, (0, lower_edge), (width, upper_edge), (0, 255, 0), 4)

                # Calculate the move speed as a ratio of the distance between the bounding box edge and frame edge.
                print("Average Height: " + str(val11))
                print("Zout threshold: " + str(.85 * height))
                if lrmiddle < l_edge:
                    self.move.set_speed(
                        self.calculate_pan_speed(0, l_edge - lrmiddle, l_edge)
                    )
                    self.move.set_direction(Move.LEFT)
                elif lrmiddle > r_edge:
                    self.move.set_speed(
                         self.calculate_pan_speed(0, lrmiddle - r_edge, l_edge)
                    )
                    self.move.set_direction(Move.RIGHT)
                elif udmiddle < lower_edge:
                    self.move.set_speed(
                        self.calculate_tilt_speed(0, lower_edge - udmiddle, lower_edge)
                    )
                    self.move.set_direction(Move.DOWN)
                elif udmiddle > upper_edge:
                    self.move.set_speed(
                         self.calculate_tilt_speed(0, udmiddle - upper_edge, lower_edge)
                    )
                    self.move.set_direction(Move.UP)
                elif val11 >= (.75 * height):
                      self.move.set_direction(Move.ZOUT)
                      self.zooming = True

                elif val11 <= (.55 * height):
                      self.move.set_direction(Move.ZIN)
                      self.zooming = True
                else:
                    if self.zooming:
                        self.move.set_direction(Move.ZSTOP)
                        self.move.do_move()
                        self.zooming = False
                    self.move.set_speed(self.args.speed_min)
                    self.move.set_direction(Move.STOP)
            else:
                
                self.move.set_direction(Move.STOP)

            # If either the speed or direction have changed then send the move command to the camera
            if self.move.direction != last_direction or self.move.speed != last_speed:
                if self.control_camera:
                    self.move.do_move()
                last_direction = self.move.direction
                last_speed = self.move.speed

            # Showing the output Image
            if self.args.ui:
                if self.show_ui(frame):
                    break
            #write frame to v4l2 loopback
            self.odevice.write(frame)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Automatic control of a PTZ camera using image recognition"
    )
    parser.add_argument(
        "visca_ip",
        help="IP address of the VISCA interface for the camera to be controlled. Required.",
    )
    parser.add_argument(
        "-p",
        "--visca_port",
        default=52381,
        help="Port number of the VISCA interface for the camera to be controlled (default: %(default)s)",
    )
    parser.add_argument(
        "-m",
        "--mqtt",
        action="store_true",
        help="Enable remote control over MQTT (default: %(default)s)",
    )
    parser.add_argument(
        "--mqtt_host",
        default="127.0.0.1",
        help="Hostname or IP of the MQTT Broker (default: %(default)s)",
    )
    parser.add_argument(
        "-c",
        "--control",
        action="store_true",
        help="If provided and MQTT is enabled, will start controlling the camera on launch, otherwise program will wait for a control command over MQTT. (default: %(default)s)",
    )
    parser.add_argument(
        "-b",
        "--boundary",
        default=0.35,
        type=float,
        help="Width of the target box to keep the tracked person inside, as a percentage of screen width (default: %(default)s)",
    )
    parser.add_argument(
        "-s",
        "--speed_min",
        default=1,
        type=int,
        help="Minimum speed to move the camera at. Not more than speed_max, min 1. (default: %(default)s)",
    )
    parser.add_argument(
        "-S",
        "--speed_max",
        default=12,
        type=int,
        help="Maximum speed to move the camera at. Not less than speed_min, max 24. (default: %(default)s)",
    )
    parser.add_argument(
        "--net_resolution",
        default="-1x128",
        help="Argument for net_resolution passed directly to OpenCV (default: %(default)s)",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="If provided, display a UI interface visualising the processing (default: %(default)s)",
    )
    parser.add_argument(
        "-v",
        "--video_source",
        default="device",
        help="Type of video input source to use. 'device' for web camera/v4l2, or 'ndi' for NDI input. (default: %(default)s)",
    )
    parser.add_argument(
        "--video_device",
        default=0,
        type=int,
        help="Video device number to read frames from (default: %(default)s)",
    )
    parser.add_argument(
        "--ndi_source",
        default=None,
        help="NDI device/source name. Required if using NDI.",
    )
    parser.add_argument(
        "--ndi_extra_ips",
        default=None,
        help="Device IPs to pass to NDI. Generally required when using docker as multicast packets can't be receieved.",
    )
    args = parser.parse_args()

    ptztrack = PTZTrack(args)

    if args.mqtt:
        ptztrack.control_camera = args.control
    else:
        ptztrack.control_camera = True

    ptztrack.main_loop()
    ptztrack.sigint_handler(None, None)  # force tidy exit
