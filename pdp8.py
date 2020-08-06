#
# PDP-8 emulator, (c) 2020 j. dersch
#
# This is a quick and dirty PDP-8 emulator written in Python because
# I needed an excuse to play around with Python a bit.
# It emulates a standard 4K PDP-8 system with no EAE, and the standard
# TTY interface.  It works well enough to run CHEKMO-II and that's
# good enough for me
#
from array import array
from signal import signal, SIGINT
import sys

import os
if os.name == 'nt':
    import msvcrt
else:
    import termios
    import tty
    from select import select


class TeletypeKeyboard:
    """
    Implements IOTs and host input functionality for the standard PDP-8
    teletype keyboard interface.
    At this time it uses msvcrt for keyboard input, which is not portable.
    """
    def __init__(self):
        self._flag = False
        self._char = 0
        self._charReady = False
        self._ie = False
        self._paperTapeLoaded = False        

    def IOT(self, opcode, ac):
        """
        Executes the IOT specified by the opcode value.
        """
        # Dispatch based on low 3 bits
        function = opcode & 0o7

        # return data is a tuple of:
        # skip, clearac, data
        # (skip indicates that a skip should take place,
        #  clearac indicates that the AC
        #  should be cleared prior to data OR'ing
        #  and data contains the data.
        skip = False
        clearac = False
        data = 0

        if function == 0:               # KCF
            self._flag = False

        if function == 1:               # KSF
            if self._flag:
                skip = True

        if function == 2:               # KCC
            clearac = True
            self._flag = False            

        if function == 4:               # KRS            
            data = self._char

        if function == 6:               # KRB
            self._flag = False            
            data = self._char            
            clearac = True

        return skip, clearac, data

    def pollCharacter(self):
        """
        Checks the host keyboard to see if there's a keystroke waiting.
        If there is one, and the TTY interface isn't already busy,
        reads it and makes it available in '_char'
        """
        if not self._charReady:
            if os.name == 'nt':         
                if msvcrt.kbhit():
                    newChar = msvcrt.getch()[0]
                    self._charReady = True
                    self._char = newChar & 0o177
            else:
                dr,dw,de = select([sys.stdin], [], [], 0)
                if dr != []:
                    newChar = sys.stdin.read(1)
                    if newChar == '\n':
                        newChar = '\r'
                    self._charReady = True
                    self._char = ord(newChar) & 0o177           

    def clock(self):
        """
        Clocks the Keyboard input logic:
        Reads any pending data from either an attached  paper-tape image file or
        the host keyboard.
        """
        if self._paperTapeLoaded:
            # Read the next character from the paper tape file
            if not self._flag:                
                data = self._paperTapeFile.read(1)

                if data == b'':
                    self._paperTapeLoaded = False
                    self._char = 0
                    print(" ** end of paper tape **")
                else:
                    self._char = data[0]
                    self._flag = True;                        

        else:
            # Read a character from the keyboard (if any)
            self.pollCharacter()
            
            if (not self._flag) and self._charReady:                
                self._flag = True;                    
                self._timer = 0
                self._charReady = False

    def attachPaperTape(self, path):
        """ Attach a paper tape image to the TTY interface """
        self._paperTapeFile = open(path, "rb")
        self._paperTapeLoaded = True

    def detachPaperTape(self):
        """ Detaches the current paper tape image from the TTY interface """
        self._paperTapeFile.close()
        self._paperTapeLoaded = False
    

class TeletypePrinter:
    """
    Implements IOTs and host input functionality for the standard PDP-8
    teletype printer interface.    
    """
    def __init__(self):
        self._flag = True
        self._outputPending = False
        self._interrupt = False        

    def IOT(self, opcode, ac):
        """
        Executes the IOT specified by the opcode value.
        """
        # Dispatch based on low 3 bits
        function = opcode & 0o7

        # return data is a tuple of:
        # skip, clearac, data
        # (skip indicates that a skip should take place,
        #  clearac indicates that the AC
        #  should be cleared prior to data OR'ing
        #  and data contains the data.
        skip = False
        
        if function == 0:               # TFL
            self._flag = True 

        if function == 1:               # TSF            
            if (self._flag):     
                skip = True

        if function == 2:               # TCF            
            self._flag = False
            self._interrupt = False

        if function == 4:               # TPC
            self._outputPending = True
            sys.stdout.write(chr(ac & 0o177))
            sys.stdout.flush()            

        if function == 6:               # TLS            
            self._flag = False
            self._interrupt = False
            self._outputPending = True
            sys.stdout.write(chr(ac & 0o177))
            sys.stdout.flush()                

        return skip, False, 0

    def clock(self):
        """
        Clocks the Printer output logic:
        Sets the "output ready" flag as necessary.
        """
        if not self._flag and self._outputPending:            
            self._flag = True
            self._interrupt = True
            self._outputPending = False
            self._timer = 0            

class PDP8:
    """
    Implements a standard 4K PDP-8 system without EAE.
    Provides the standard PDP-8 teletype interface.    
    """
    def __init__(self):
        # Allocate 4096 bytes for the system memory
        # (No memory extension support yet)
        self._memory = array('H', [0] * 4096)
        self._pc = 0
        self._ac = 0
        self._mq = 0
        self._l = 0
        self._ie = False
        self._ieCounter = 0
        self._switch = 0
        self._halted = True
        self._ioPollCounter = 0;

        self._ttyKeyboard = TeletypeKeyboard()
        self._ttyPrinter = TeletypePrinter()

        # Set up dictionary for IOT mappings:
        self._iotMap = { 0o6030 : self._ttyKeyboard,
                         0o6031 : self._ttyKeyboard,
                         0o6032 : self._ttyKeyboard,
                         0o6034 : self._ttyKeyboard,
                         0o6035 : self._ttyKeyboard,
                         0o6036 : self._ttyKeyboard,
                         0o6040 : self._ttyPrinter,
                         0o6041 : self._ttyPrinter,
                         0o6042 : self._ttyPrinter,
                         0o6044 : self._ttyPrinter,
                         0o6045 : self._ttyPrinter,
                         0o6046 : self._ttyPrinter }        
        

    def getInstruction(self):
        """ Retrieves the instruction word pointed to by the current PC """
        return self._memory[self._pc]

    
    def getEffectiveAddress(self, opcode):
        """
        Calculates the effective address specified by the provided opcode word,
        using the current PC.
        Note: this method has the side-effect of doing auto-index incrementing
         which may not be optimal.  Do not call this more than once for a
         given opcode unless you have a good reason to.
        """
        indirect = opcode & 0o400
        zeroPage = not (opcode & 0o200)
        address = opcode & 0o177

        # if this is a zero-page address, we take the address as-is,
        # otherwise it's the address in the current field.
        if not zeroPage:
            address = (self._pc & 0o7600) | address        

        if indirect:
            # pre-increment auto-index indirect word between o10 and o17.            
            if address >= 0o10 and address < 0o20:
                self._memory[address] = (self._memory[address] + 1) & 0o7777
                
            return self._memory[address]
        else:
            return address

    def getArg(self, opcode):
        """ Retrieves the word pointed to by the provided opcode word. """
        return self._memory[self.getEffectiveAddress(opcode)]    

    def putArg(self, opcode, arg):
        """
        Stores the value in arg at the address pointed to by the provided
        opcode word.
        """
        self._memory[self.getEffectiveAddress(opcode)] = arg & 0o7777

    def incrementPC(self):
        """ Increments the PC by 1, and clips the value to a 12-bit value. """
        self._pc += 1
        self._pc &= 0o7777

    def rar(self):
        """ Implements the 13-bit rotate-right (12 bits from AC + 1 bit in L) """
        oldL = self._l
        self._l = (self._ac & 1)
        self._ac = self._ac >> 1
        self._ac |= (oldL << 11)
        self._ac &= 0o7777

    def ral(self):
        """ Implements the 13-bit rotate-left (12 bits from AC + 1 bit in L) """
        oldL = self._l
        self._l = ((self._ac & 0o4000) >> 11) & 1
        self._ac = self._ac << 1
        self._ac |= oldL
        self._ac &= 0o7777

    def op_and(self, opcode):
        """ Implements the AND instruction """
        self._ac &= self.getArg(opcode)      

    def op_tad(self, opcode):
        """ Implements the TAD (two's complement add) instruction """
        self._ac += self.getArg(opcode)

        # handle overflow
        if (self._ac > 0o7777):
            self._l = (~self._l) & 0o1

        self._ac &= 0o7777                    

    def op_isz(self, opcode):
        """ Implements the ISZ (increment and skip if zero) instruction """
        # Increment value from memory
        arg = self.getArg(opcode)
        arg += 1
        arg &= 0o7777
        # Write it back to memory
        self.putArg(opcode, arg)

        # Skip if necessary
        if (arg == 0):
            self.incrementPC()        

    def op_dca(self, opcode):
        """ Implements the DCA (deposit and clear accumulator) instruction """
        self.putArg(opcode, self._ac)
        self._ac = 0        

    def op_jms(self, opcode):
        """ Implements the JMS (jump subroutine) instruction """
        # Get address of routine
        addr = self.getEffectiveAddress(opcode)

        # Store return address there
        self.incrementPC()
        self.putArg(opcode, self._pc)

        # Jump to the routine
        # (this is actually addr + 1, but step() does the
        # increment for us)
        self._pc = addr & 0o7777

    def op_jmp(self, opcode):
        """ Implements the JMP (jump) instruction """
        # -1 because step() increments PC
        self._pc = (self.getEffectiveAddress(opcode) - 1) & 0o7777

    def op_iot(self, opcode):
        """
        Implements the IOT (I/O Transfer) instruction.
        This dispatches to IOT routines in _iotMap, and handles IOTs
        intrinsic to the PDP-8 processor itself.
        """
        # Dispatch IOTs to devices
        if opcode in self._iotMap:
            skip, clearac, data = self._iotMap[opcode].IOT(opcode, self._ac)

            if skip:
                self.incrementPC()
                
            if clearac:
                self._ac = 0                                    

            self._ac |= data
            
        # Handle IOTs built into the processor:
        elif opcode == 0o6000:      # SKON
            if self._ie:
                self.incrementPC()
            
        elif opcode == 0o6001:      # ION
            self._ieCounter = 1                                
        elif opcode == 0o6002:      # IOF
            self._ie = False            
            
        else:            
            # Unhandled IOT, just ignore it for now.
            # print("Unhandled IOT %(iot)04o" % { "iot": opcode })
            pass

    def op_micro(self, opcode):
        """ Implements the 'microcoded' swiss-army-knife instruction class"""
        skip = False

        # Group One (111 0xx xxx)
        if (opcode & 0o7400) == 0o7000:
            # Execute in order
            if opcode & 0o200:
                self._ac = 0            # CLA
            if opcode & 0o100:
                self._l = 0             # CLL
            if opcode & 0o40:
                self._ac = (~self._ac) & 0o7777  # CMA
            if opcode & 0o20:
                self._l = (~self._l) & 0o1       # CML
            if opcode & 0o1:
                self._ac += 1           # IAC
                if (self._ac > 0o7777):
                    self._l = (~self._l & 0o1)
                    self._ac = 0
            if opcode & 0o10:
                self.rar();             # RAR                
            if opcode & 0o4:
                self.ral();             # RAL                
            if opcode & 0o2:
                if opcode & 0o10:
                    self.rar();         # RAR again (RTR)
                if opcode & 0o04:
                    self.ral();         # RAL again (RTL)

                if (opcode & 0o14) == 0:
                    self._ac = (self._ac << 6) | (self._ac >> 6)  # BSW
                    
        # Group Two (OR group)
        elif (opcode & 0o7411) == 0o7400:
            if opcode & 0o20 and self._l != 0:
                skip = True             # SNL
            if opcode & 0o40 and self._ac == 0:
                skip = True             # SZA
            if opcode & 0o100 and (self._ac & 0o4000):
                skip = True             # SMA
            if opcode & 0o200:
                self._ac = 0            # CLA

            # Privileged instructions
            # TODO: deal with time-sharing hardware
            if opcode & 0o2:
                self._halted = True
            if opcode & 0o4:
                self._ac |= self._switch
                
        # Group two (AND group)
        elif (opcode & 0o7411) == 0o7410:
            skip = True
            if opcode & 0o20:
                skip = skip & (self._l == 0)   # SZL
            if opcode & 0o40:
                skip = skip & (self._ac != 0)  # SNA
            if opcode & 0o100:
                skip = skip & ((self._ac) & 0o4000 == 0)   # SPA
            if opcode & 0o200:
                self._ac = 0            # CLA            

        # Group three
        elif (opcode & 0o7401) == 0o7401:
            # Mostly EAE-related stuff.
            # I don't emulate the EAE yet but the
            # MQA and MQL bits still function without it.
            # (on the 8/e, anyway)
            if opcode & 0o200:
                self._ac = 0

            if opcode & 0o120 == 0o120:
                oldAC = self._ac            # SWP
                self._ac = self._mq
                self._mq = oldAC
            else: 
                if opcode & 0o100:
                    self._ac |= self._mq        # MQA
                if opcode & 0o20:
                    self._mq = self._ac         # MQL
                    self._ac = 0
        else:
            # Just to catch errors in the above...
            printf("Unhandled microcoded instruction!")       

        # Skip if necessary
        if skip:
            self.incrementPC()                    

    def step(self):
        """ Executes one PDP-8 instruction, and clocks I/O logic """    
                    
        # switch on the opcode (top 3 bits)
        instruction = self.getInstruction()
        opcode = (instruction >> 9) & 0o7;

        if opcode == 0:     # AND
            self.op_and(instruction)
        elif opcode == 1:   # TAD
            self.op_tad(instruction)
        elif opcode == 2:   # ISZ
            self.op_isz(instruction)
        elif opcode == 3:   # DCA
            self.op_dca(instruction)
        elif opcode == 4:   # JMS
            self.op_jms(instruction)
        elif opcode == 5:   # JMP
            self.op_jmp(instruction)
        elif opcode == 6:   # IOT
            self.op_iot(instruction)
        elif opcode == 7:   # Microcoded
            self.op_micro(instruction)

        # Move to the next instruction
        self.incrementPC()

        # This is ugly and should go away in favor of a cleaner solution
        # if I ever extend this to provide more than just the TTY interface.
        # Every 100 clocks we check the TTY to see if I/O needs to happen.
        self._ioPollCounter += 1

        if self._ioPollCounter > 100:
            self._ioPollCounter = 0

            # clock devices
            self._ttyKeyboard.clock()
            self._ttyPrinter.clock()

            # check for interrupts from TTY (HACK MAKE MORE GENERAL)
            if self._ie and (self._ttyKeyboard._flag or self._ttyPrinter._interrupt):                
                self._memory[0] = self._pc
                self._ie = False;
                self._pc = 1   
                    

        if self._ieCounter > 0:
            self._ieCounter -= 1
            if self._ieCounter == 0:
                self._ie = True
        

    # Debugger-related stuff
    
    def printStatus(self):
        """ Prints interesting status about the PDP-8 CPU """
        print('PC %(pc)04o AC %(ac)04o L %(l)01o SW %(sw)04o IE %(ie)01o' %
              {'pc': self._pc, 'ac': self._ac, 'l': self._l, 'sw': self._switch, 'ie': self._ie })

    def deposit(self, address, data):
        """ Stows the word in data at the specified address """
        if address < len(self._memory) and address >=0:
            self._memory[address] = data & 0o7777

    def examine(self, address):
        """ Examines data at the specified address """
        if address < len(self._memory) and address >= 0:
            print('%(address)04o : %(data)04o' % {'address': address, 'data': self._memory[address] })
        else:
            print('Invalid address')

def captureTerm():
    global prev_term
    if os.name != 'nt':
        stdin_fd = sys.stdin.fileno()
        prev_term = termios.tcgetattr(stdin_fd)
        tty.setcbreak(stdin_fd)

def releaseTerm():
    if os.name != 'nt':
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, prev_term)

def runDebugger():
    """ Runs an incredibly crude command prompt allowing basic manipulation of memory and the CPU """
    cpu = PDP8()

    # Set up some stuff so we can trap Ctrl+C to stop execution of the processor.
    def breakHandler(signal, frame):
        print("CTRL-C halt")
        releaseTerm()
        cpu._halted = True
    
    signal(SIGINT, breakHandler)    

    print("PDP-8 simulator v0.000001, (c) 2020 j. dersch")    

    while True:
        # Run a simple debugger prompt:
            cpu.printStatus()
            print(">", end = " ")
            cmdLine = input()
            tokens = cmdLine.split()
            error = False

            if len(tokens) == 0:
                continue
            
            # Hi.  Why does Python not have a "switch" equivalent.
            #
            command = tokens[0]

            try:
                # q - Quit the emulator
                if command == "q":
                    break

                # s - Single-step the processor
                elif command == "s":
                    cpu.step()

                # r - Run the processor from the current PC
                elif command == "r":
                    cpu._halted = False
                    captureTerm()
                    while (not cpu._halted):
                        cpu.step()
                    releaseTerm()

                # d - Deposit a value into memory
                # (usage: "d <addr> <value>")
                elif command == "d":
                    if len(tokens) == 3:
                        address = int(tokens[1], base=8)
                        data = int(tokens[2], base=8)
                        cpu.deposit(address,data)
                    else:
                        error = True

                # e - Examine the contents of memory
                # (usage: "e <addr>")
                elif command == "e":
                    if len(tokens) == 2:
                        address = int(tokens[1], base=8)
                        cpu.examine(address)
                    else:
                        error = True

                # ac - Sets the value of the AC register
                # (usage: "ac <value>")
                elif command == "ac":
                    if len(tokens) == 2:
                        cpu._ac = int(tokens[1], base=8) & 0o7777
                    else:
                        error = True

                # l - Sets the value of the Link register
                # (usage: "l <value>")
                elif command == "l":
                    if len(tokens) == 2:
                        cpu._l = int(tokens[1], base=8) & 0o1
                    else:
                        error = True

                # pc - Sets the value of the PC register
                # (usage: "pc <value>")
                elif command == "pc":
                    if len(tokens) == 2:
                        cpu._pc = int(tokens[1], base=8) & 0o7777
                    else:
                        error = True

                # sw - Sets the value of the front panel switch register
                # (usage: "sw <value>")
                elif command == "sw":
                    if len(tokens) == 2:
                        cpu._switch = int(tokens[1], base=8) & 0o7777
                    else:
                        error = True           

                # rim - Loads the standard low-speed paper-tape RIM loader into memory
                #       at address 7756.
                elif command == "rim":
                    if len(tokens) == 1:                    
                        rimLoader = array('H',
                                          [0o6032, 0o6031, 0o5357, 0o6036,
                                           0o7106, 0o7006, 0o7510, 0o5357,
                                           0o7006, 0o6031, 0o5367, 0o6034,
                                           0o7420, 0o3776, 0o3376, 0o5356 ])

                        cpu._memory[0o7756:0o7776] = rimLoader

                # pt - Attaches a paper tape image file to the TTY interface,
                #      or detaches the current image from same.
                # (usage: "pt <image file>" to attach,
                #         "pt" to detach.
                elif command == "pt":
                    if len(tokens) == 2:
                        cpu._ttyKeyboard.attachPaperTape(tokens[1])
                    elif len(tokens) == 1:
                        cpu._ttyKeyboard.detachPaperTape()
                    else:
                        error = True
                        
                else:
                    error = True
            except:
                    error = True

            if error:
                print("?")  # Ken would approve
                
                    
def main():
    runDebugger()

if __name__=="__main__": 
    main()
